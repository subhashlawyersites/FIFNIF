import os
import time
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, time as dtime, timedelta, timezone
import pandas as pd
from fyers_apiv3 import fyersModel

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NSE:NIFTY50-INDEX"
CACHE_DIR = "nifty_cache"
START_DATE = "2025-11-22"
END_DATE = "2026-11-22"
TARGET_LEVELS = [1.0, 0.8, 0.7, 0.6, 0.5]


def load_or_fetch_candles(fyers: fyersModel.FyersModel, resolution: str) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    csv_path = os.path.join(CACHE_DIR, f"nifty50_{resolution}m.csv")

    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        print(f"[{resolution}m] Loaded {len(df)} cached candles from {csv_path}")
        return df

    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").date()
    all_rows = []
    cur = start_dt

    while cur <= end_dt:
        chunk_end = min(cur + timedelta(days=89), end_dt)
        resp = fyers.history({
            "symbol": SYMBOL,
            "resolution": resolution,
            "date_format": "1",
            "range_from": cur.strftime("%Y-%m-%d"),
            "range_to": chunk_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        })
        if resp.get("s") == "ok" and resp.get("candles"):
            all_rows.extend(resp["candles"])
            print(f"[{resolution}m] Fetched {cur} -> {chunk_end} ({len(resp['candles'])} bars)")
        else:
            print(f"[{resolution}m] Warning {cur} -> {chunk_end}: {resp}")
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.35)

    df = pd.DataFrame(all_rows, columns=["epoch", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["epoch"]).sort_values("epoch").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert(IST)
    df = df[
        (df["timestamp"].dt.time >= dtime(9, 15))
        & (df["timestamp"].dt.time <= dtime(15, 30))
    ].copy()
    df[["epoch", "timestamp", "open", "high", "low", "close", "volume"]].to_csv(csv_path, index=False)
    print(f"[{resolution}m] Saved {len(df)} session candles -> {csv_path}")
    return df


def backtest_variant(
    df: pd.DataFrame,
    max_trades_per_day: int,
    require_color: bool,
    target_pct: float,
) -> dict:
    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)

    work["sma2"] = work["close"].rolling(2).mean()
    work["sma10"] = work["close"].rolling(10).mean()
    work["session_date"] = work["timestamp"].dt.date
    work["bar_time"] = work["timestamp"].dt.time

    sessions = work.groupby("session_date").agg(
        s_open=("open", "first"),
        s_close=("close", "last"),
    )
    sessions["prev_close"] = sessions["s_close"].shift(1)
    sessions["gap_pct"] = (sessions["s_open"] - sessions["prev_close"]) / sessions["prev_close"]
    excluded_dates = set(sessions[sessions["gap_pct"] <= -0.0075].index)

    entry_earliest = dtime(9, 45)
    cutoff_time = dtime(15, 15)
    trades = []

    for s_date, day_df in work.groupby("session_date"):
        if s_date in excluded_dates:
            continue

        idxs = day_df.index.tolist()
        if len(idxs) < 4:
            continue

        trades_today = 0
        in_trade = False
        trade = None
        armed_dir = None
        ref_sl = None

        for bar_num, g_idx in enumerate(idxs):
            if g_idx == 0:
                continue

            bar = work.loc[g_idx]
            prev = work.loc[g_idx - 1]
            t_now = bar["bar_time"]

            # 1. Manage open trade first (no overlapping trades)
            if in_trade and trade is not None:
                d = trade["direction"]
                sl = trade["sl"]
                tgt = trade["target"]

                hit_sl = (bar["low"] <= sl) if d == "CALL" else (bar["high"] >= sl)
                hit_tgt = (bar["high"] >= tgt) if d == "CALL" else (bar["low"] <= tgt)

                exit_reason = None
                exit_px = None

                if hit_sl and hit_tgt:
                    exit_reason = "SL_AMBIGUOUS"
                    exit_px = sl
                elif hit_sl:
                    exit_reason = "SL"
                    exit_px = sl
                elif hit_tgt:
                    exit_reason = "TARGET"
                    exit_px = tgt
                elif t_now >= cutoff_time or bar_num == len(idxs) - 1:
                    exit_reason = "EOD_1515"
                    exit_px = bar["close"]

                if exit_reason is not None:
                    pts = (exit_px - trade["entry"]) if d == "CALL" else (trade["entry"] - exit_px)
                    r_val = (pts / trade["risk"]) if trade["risk"] > 0 else 0.0
                    trade["pnl_pts"] = pts
                    trade["r_val"] = r_val
                    trade["exit_reason"] = exit_reason
                    trades.append(trade)
                    in_trade = False
                    trade = None
                    armed_dir = None
                    ref_sl = None

            # 2. Most recent crossover updates armed setup (when not currently in a trade)
            if (
                not in_trade
                and pd.notna(prev["sma2"])
                and pd.notna(prev["sma10"])
                and pd.notna(bar["sma2"])
                and pd.notna(bar["sma10"])
            ):
                up_cross = (prev["sma2"] <= prev["sma10"]) and (bar["sma2"] > bar["sma10"])
                dn_cross = (prev["sma2"] >= prev["sma10"]) and (bar["sma2"] < bar["sma10"])

                if up_cross:
                    armed_dir = "CALL"
                    ref_sl = float(prev["low"])
                    continue
                if dn_cross:
                    armed_dir = "PUT"
                    ref_sl = float(prev["high"])
                    continue

            # 3. Entry candle after first 3 candles (>= 09:45) and before 15:15 cutoff
            if (
                not in_trade
                and trades_today < max_trades_per_day
                and armed_dir is not None
                and bar_num >= 3
                and entry_earliest <= t_now < cutoff_time
            ):
                entry_px = float(bar["close"])
                upper_wick = float(bar["high"]) - max(float(bar["open"]), entry_px)
                lower_wick = min(float(bar["open"]), entry_px) - float(bar["low"])
                sma10_val = float(bar["sma10"])

                if armed_dir == "CALL":
                    color_ok = (entry_px > float(bar["open"])) if require_color else True
                    no_touch = float(bar["low"]) > sma10_val
                    wick_ok = lower_wick > upper_wick
                    risk = entry_px - ref_sl
                    tgt_px = entry_px + (target_pct * risk)
                else:
                    color_ok = (entry_px < float(bar["open"])) if require_color else True
                    no_touch = float(bar["high"]) < sma10_val
                    wick_ok = upper_wick > lower_wick
                    risk = ref_sl - entry_px
                    tgt_px = entry_px - (target_pct * risk)

                if wick_ok and no_touch and color_ok and risk > 0:
                    in_trade = True
                    trades_today += 1
                    trade = {
                        "direction": armed_dir,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                    }

    if not trades:
        return {
            "max_trades": max_trades_per_day,
            "color_req": require_color,
            "target_pct": int(target_pct * 100),
            "trades": 0,
            "win_rate_%": 0.0,
            "net_pts": 0.0,
            "net_r": 0.0,
            "avg_r": 0.0,
        }

    tdf = pd.DataFrame(trades)
    wins = int((tdf["pnl_pts"] > 0).sum())
    return {
        "max_trades": max_trades_per_day,
        "color_req": require_color,
        "target_pct": int(target_pct * 100),
        "trades": int(len(tdf)),
        "win_rate_%": round(100.0 * wins / len(tdf), 2),
        "net_pts": round(float(tdf["pnl_pts"].sum()), 1),
        "net_r": round(float(tdf["r_val"].sum()), 2),
        "avg_r": round(float(tdf["r_val"].mean()), 3),
    }


def main():
    client_id = os.getenv("FYERS_CLIENT_ID", "YOUR_APP_ID-100")
    access_token = os.getenv("FYERS_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
    fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")

    for tf in ["10", "15", "5"]:
        df = load_or_fetch_candles(fyers, tf)
        if df.empty:
            continue
        rows = []
        for max_tr in [1, 2]:
            for color_req in [True, False]:
                for tgt in TARGET_LEVELS:
                    rows.append(backtest_variant(df, max_tr, color_req, tgt))
        out = pd.DataFrame(rows)
        print(f"\n================ NIFTY 50 {tf}m BACKTEST ({len(df)} candles) ================")
        print(out.to_string(index=False))


if __name__ == "__main__":
    main()