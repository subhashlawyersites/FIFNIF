from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles


def analyze_15m_targets_and_mfe(max_trades_per_day: int = 2, require_color: bool = True):
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
    cutoff_time = dtime(15, 15)
    target_levels = [0.6, 0.7, 0.8, 0.9, 1.0]

    # Collect raw trades with peak favorable excursion while open
    raw_trades = []
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
        best_fav_pts = 0.0

        for bar_num, g_idx in enumerate(idxs):
            if g_idx == 0:
                continue
            bar = work.loc[g_idx]
            prev = work.loc[g_idx - 1]
            t_now = bar["bar_time"]

            if in_trade and trade is not None:
                d = trade["direction"]
                fav_move = (float(bar["high"]) - trade["entry"]) if d == "CALL" else (trade["entry"] - float(bar["low"]))
                if fav_move > best_fav_pts:
                    best_fav_pts = fav_move

                # Close at 15:15 or last bar of day to measure total favorable excursion
                if t_now >= cutoff_time or bar_num == len(idxs) - 1:
                    trade["max_fav_pts"] = max(0.0, best_fav_pts)
                    trade["max_fav_r"] = (trade["max_fav_pts"] / trade["risk"]) if trade["risk"] > 0 else 0.0
                    raw_trades.append(trade)
                    in_trade = False
                    trade = None
                    armed_dir = None
                    ref_sl = None
                    best_fav_pts = 0.0

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
                else:
                    color_ok = entry_px < float(bar["open"]) if require_color else True
                    no_touch = float(bar["high"]) < sma10_val
                    wick_ok = upper_wick > lower_wick
                    risk = ref_sl - entry_px

                if wick_ok and no_touch and color_ok and risk > 0:
                    in_trade = True
                    trades_today += 1
                    best_fav_pts = 0.0
                    trade = {
                        "direction": armed_dir,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "risk": risk,
                    }

    rtdf = pd.DataFrame(raw_trades)
    print("\n=== 1. TARGET-SIDE FAVORABLE MOVEMENT (MFE BEFORE 15:15) ===")
    print(f"Total Entries : {len(rtdf)}")
    print(f"Avg Max Fav R : {round(float(rtdf['max_fav_r'].mean()), 3)} R | Median Max Fav R: {round(float(rtdf['max_fav_r'].median()), 3)} R")
    print(f"Avg Max Fav Pt: {round(float(rtdf['max_fav_pts'].mean()), 1)} pts | Median Max Fav Pt: {round(float(rtdf['max_fav_pts'].median()), 1)} pts")
    for lvl in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.25, 1.5]:
        pct_reached = round(100.0 * float((rtdf["max_fav_r"] >= lvl).mean()), 2)
        cnt_reached = int((rtdf["max_fav_r"] >= lvl).sum())
        print(f"  Reached >= {lvl:.2f}R : {cnt_reached}/{len(rtdf)} trades ({pct_reached}%)")

    # 2. Simulate exact SL / Target / 15:15 exits across 60%, 70%, 80%, 90%, 100%
    sim_rows = []
    for tgt_pct in target_levels:
        sim_trades = []
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
                        sim_trades.append({"pnl_pts": pts, "r_val": r_val, "exit_reason": exit_reason})
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
                        tgt_px = entry_px + (tgt_pct * risk)
                    else:
                        color_ok = entry_px < float(bar["open"]) if require_color else True
                        no_touch = float(bar["high"]) < sma10_val
                        wick_ok = upper_wick > lower_wick
                        risk = ref_sl - entry_px
                        tgt_px = entry_px - (tgt_pct * risk)

                    if wick_ok and no_touch and color_ok and risk > 0:
                        in_trade = True
                        trades_today += 1
                        trade = {"direction": armed_dir, "entry": entry_px, "sl": ref_sl, "target": tgt_px, "risk": risk}

        sdf = pd.DataFrame(sim_trades)
        wins = int((sdf["pnl_pts"] > 0).sum())
        tgt_hits = int((sdf["exit_reason"] == "TARGET").sum())
        sim_rows.append({
            "target_pct": int(tgt_pct * 100),
            "trades": len(sdf),
            "win_rate_%": round(100.0 * wins / len(sdf), 2),
            "target_hit_%": round(100.0 * tgt_hits / len(sdf), 2),
            "net_pts": round(float(sdf["pnl_pts"].sum()), 1),
            "net_r": round(float(sdf["r_val"].sum()), 2),
            "avg_r": round(float(sdf["r_val"].mean()), 3),
        })

    print("\n=== 2. TARGET EXIT COMPARISON (60%, 70%, 80%, 90%, 100%) ON 15m ===")
    print(pd.DataFrame(sim_rows).to_string(index=False))


if __name__ == "__main__":
    analyze_15m_targets_and_mfe(max_trades_per_day=2, require_color=True)
