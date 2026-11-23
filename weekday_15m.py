from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles


def weekday_breakdown_15m(max_trades_per_day: int = 2, require_color: bool = True, target_pct: float = 0.6):
    df = load_or_fetch_candles(None, "15")
    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)

    work["sma2"] = work["close"].rolling(2).mean()
    work["sma10"] = work["close"].rolling(10).mean()
    work["session_date"] = work["timestamp"].dt.date
    work["bar_time"] = work["timestamp"].dt.time
    work["weekday"] = work["timestamp"].dt.day_name()

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
        weekday_name = day_df["weekday"].iloc[0]

        for bar_num, g_idx in enumerate(idxs):
            if g_idx == 0:
                continue
            bar = work.loc[g_idx]
            prev = work.loc[g_idx - 1]
            t_now = bar["bar_time"]

            if in_trade and trade is not None:
                d = trade["direction"]
                sl = trade["sl"]
                tgt = trade["target"]
                hit_sl = (bar["low"] <= sl) if d == "CALL" else (bar["high"] >= sl)
                hit_tgt = (bar["high"] >= tgt) if d == "CALL" else (bar["low"] <= tgt)

                exit_reason = None
                exit_px = None
                if hit_sl and hit_tgt:
                    exit_reason, exit_px = "SL_AMBIGUOUS", sl
                elif hit_sl:
                    exit_reason, exit_px = "SL", sl
                elif hit_tgt:
                    exit_reason, exit_px = "TARGET", tgt
                elif t_now >= cutoff_time or bar_num == len(idxs) - 1:
                    exit_reason, exit_px = "EOD_1515", bar["close"]

                if exit_reason is not None:
                    pts = (exit_px - trade["entry"]) if d == "CALL" else (trade["entry"] - exit_px)
                    r_val = (pts / trade["risk"]) if trade["risk"] > 0 else 0.0
                    trade["pnl_pts"] = pts
                    trade["r_val"] = r_val
                    trades.append(trade)
                    in_trade = False
                    trade = None
                    armed_dir = None
                    ref_sl = None

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
                    color_ok = entry_px > float(bar["open"]) if require_color else True
                    no_touch = float(bar["low"]) > sma10_val
                    wick_ok = lower_wick > upper_wick
                    risk = entry_px - ref_sl
                    tgt_px = entry_px + (target_pct * risk)
                else:
                    color_ok = entry_px < float(bar["open"]) if require_color else True
                    no_touch = float(bar["high"]) < sma10_val
                    wick_ok = upper_wick > lower_wick
                    risk = ref_sl - entry_px
                    tgt_px = entry_px - (target_pct * risk)

                if wick_ok and no_touch and color_ok and risk > 0:
                    in_trade = True
                    trades_today += 1
                    trade = {
                        "date": str(s_date),
                        "weekday": weekday_name,
                        "direction": armed_dir,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                        "pnl_pts": 0.0,
                        "r_val": 0.0,
                    }

    tdf = pd.DataFrame(trades)
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    by_trade = (
        tdf.groupby("weekday", as_index=False)
        .agg(
            trades=("pnl_pts", "count"),
            win_rate_pct=("pnl_pts", lambda s: round(100.0 * (s > 0).mean(), 2)),
            net_pts=("pnl_pts", lambda s: round(float(s.sum()), 1)),
            net_r=("r_val", lambda s: round(float(s.sum()), 2)),
            avg_r=("r_val", lambda s: round(float(s.mean()), 3)),
        )
    )

    daily = (
        tdf.groupby(["date", "weekday"], as_index=False)
        .agg(day_pts=("pnl_pts", "sum"), day_r=("r_val", "sum"))
    )
    by_day = (
        daily.groupby("weekday", as_index=False)
        .agg(
            active_days=("day_pts", "count"),
            green_days=("day_pts", lambda s: int((s > 0).sum())),
            red_days=("day_pts", lambda s: int((s < 0).sum())),
        )
    )

    out = pd.merge(by_trade, by_day, on="weekday", how="left")
    out["weekday"] = pd.Categorical(out["weekday"], categories=order, ordered=True)
    out = out.sort_values("weekday").reset_index(drop=True)

    print("\n=== 15m PERFORMANCE BY WEEKDAY (MON-FRI | max_trades=2, color_req=True, target=60%) ===")
    print(out.to_string(index=False))


if __name__ == "__main__":
    weekday_breakdown_15m(max_trades_per_day=2, require_color=True, target_pct=0.6)
