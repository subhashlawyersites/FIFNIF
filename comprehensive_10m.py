from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles


def run_10m_backtest(
    max_trades_per_day: int = 2,
    require_color: bool = True,
    target_pct: float = 0.60,
    df_10m: pd.DataFrame | None = None,
) -> pd.DataFrame:
    df = df_10m if df_10m is not None else load_or_fetch_candles(None, "10")
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
                    session_slot = "MORNING (<12:00)" if t_now < dtime(12, 0) else "AFTERNOON (>=12:00)"
                    trade = {
                        "date": str(s_date),
                        "weekday": weekday_name,
                        "entry_time": str(t_now),
                        "session_slot": session_slot,
                        "direction": armed_dir,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                        "pnl_pts": 0.0,
                        "r_val": 0.0,
                        "exit_reason": "",
                    }

    return pd.DataFrame(trades)


def summarize_subset(sub: pd.DataFrame, label: str) -> dict:
    if len(sub) == 0:
        return {"subset": label, "trades": 0, "win_rate_%": 0.0, "net_pts": 0.0, "net_r": 0.0, "avg_r": 0.0, "max_dd_pts": 0.0, "max_dd_r": 0.0, "max_loss_streak": 0}
    cum_pts = sub["pnl_pts"].cumsum()
    dd_pts = round(float((cum_pts - cum_pts.cummax()).min()), 1)
    cum_r = sub["r_val"].cumsum()
    dd_r = round(float((cum_r - cum_r.cummax()).min()), 2)

    streak = 0
    max_streak = 0
    for pnl in sub["pnl_pts"]:
        if pnl <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "subset": label,
        "trades": len(sub),
        "win_rate_%": round(100.0 * (sub["pnl_pts"] > 0).mean(), 2),
        "net_pts": round(float(sub["pnl_pts"].sum()), 1),
        "net_r": round(float(sub["r_val"].sum()), 2),
        "avg_r": round(float(sub["r_val"].mean()), 3),
        "max_dd_pts": dd_pts,
        "max_dd_r": dd_r,
        "max_loss_streak": max_streak,
    }


if __name__ == "__main__":
    df_10m = load_or_fetch_candles(None, "10")
    tdf = run_10m_backtest(max_trades_per_day=2, require_color=True, target_pct=0.60, df_10m=df_10m)

    print("\n=== 1. OVERALL 10M: MON-FRI VS MON-THU ===")
    over_rows = [
        summarize_subset(tdf, "10m Mon-Fri (All)"),
        summarize_subset(tdf[tdf["weekday"] != "Friday"].reset_index(drop=True), "10m Mon-Thu (Excl Fri)"),
    ]
    print(pd.DataFrame(over_rows).to_string(index=False))

    print("\n=== 2. 10M BY DIRECTION & SESSION (MON-FRI) ===")
    ds_rows = []
    for d in ["CALL", "PUT"]:
        ds_rows.append(summarize_subset(tdf[tdf["direction"] == d].reset_index(drop=True), f"Dir: {d}"))
    for slot in ["MORNING (<12:00)", "AFTERNOON (>=12:00)"]:
        ds_rows.append(summarize_subset(tdf[tdf["session_slot"] == slot].reset_index(drop=True), f"Session: {slot}"))
    print(pd.DataFrame(ds_rows).to_string(index=False))

    print("\n=== 3. 10M TARGET % SWEEP (MON-FRI) ===")
    tgt_rows = []
    for pct in [0.50, 0.60, 0.70, 0.80, 1.00]:
        sub_t = run_10m_backtest(max_trades_per_day=2, require_color=True, target_pct=pct, df_10m=df_10m)
        row = summarize_subset(sub_t, f"Target {int(pct*100)}% (0.6R-> {pct:.1f}R)")
        tgt_rows.append(row)
    print(pd.DataFrame(tgt_rows).to_string(index=False))

    print("\n=== 4. 10M BY WEEKDAY (MON-FRI) ===")
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    wk_rows = []
    for wd in order:
        wk_rows.append(summarize_subset(tdf[tdf["weekday"] == wd].reset_index(drop=True), wd))
    print(pd.DataFrame(wk_rows).to_string(index=False))
