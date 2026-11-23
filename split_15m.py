from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles


def run_15m_splits(max_trades_per_day: int = 2, require_color: bool = True, target_pct: float = 0.6):
    df = load_or_fetch_candles(None, "15")
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
    noon_split = dtime(12, 0)
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
                    trade["exit_reason"] = exit_reason
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
                    session_bucket = "Morning (09:45-11:59)" if t_now < noon_split else "Afternoon (12:00-15:14)"
                    trade = {
                        "direction": armed_dir,
                        "entry_time": str(t_now),
                        "session": session_bucket,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                    }

    tdf = pd.DataFrame(trades)
    for col in ["direction", "session"]:
        grp = (
            tdf.groupby(col, as_index=False)
            .agg(
                trades=("pnl_pts", "count"),
                win_rate_pct=("pnl_pts", lambda s: round(100.0 * (s > 0).mean(), 2)),
                net_pts=("pnl_pts", lambda s: round(float(s.sum()), 1)),
                net_r=("r_val", lambda s: round(float(s.sum()), 2)),
                avg_r=("r_val", lambda s: round(float(s.mean()), 3)),
            )
        )
        print(f"\n=== 15m Breakdown by {col} (max_trades={max_trades_per_day}, color_req={require_color}, target={int(target_pct*100)}%) ===")
        print(grp.to_string(index=False))


if __name__ == "__main__":
    run_15m_splits(max_trades_per_day=2, require_color=True, target_pct=0.6)