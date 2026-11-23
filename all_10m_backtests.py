from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles, TARGET_LEVELS


def _prep_10m() -> pd.DataFrame:
    df = load_or_fetch_candles(None, "10")
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
    return work[~work["session_date"].isin(excluded_dates)].reset_index(drop=True)


def _metrics(tdf: pd.DataFrame) -> dict:
    if tdf.empty:
        return {"trades": 0, "win_rate_%": 0.0, "net_pts": 0.0, "net_r": 0.0, "avg_r": 0.0}
    wins = int((tdf["pnl_pts"] > 0).sum())
    return {
        "trades": int(len(tdf)),
        "win_rate_%": round(100.0 * wins / len(tdf), 2),
        "net_pts": round(float(tdf["pnl_pts"].sum()), 1),
        "net_r": round(float(tdf["r_val"].sum()), 2),
        "avg_r": round(float(tdf["r_val"].mean()), 3),
    }


def run_new_10m_trades(work: pd.DataFrame, max_trades_per_day: int, require_color: bool, target_pct: float) -> pd.DataFrame:
    entry_earliest = dtime(9, 45)
    noon_split = dtime(12, 0)
    cutoff_time = dtime(15, 15)
    trades = []

    for _, day_df in work.groupby("session_date"):
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
                    session_bucket = "Morning (09:45-11:59)" if t_now < noon_split else "Afternoon (12:00-15:14)"
                    trade = {
                        "direction": armed_dir,
                        "session": session_bucket,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                    }

    return pd.DataFrame(trades)


def run_prev_10m_trades(work: pd.DataFrame, exclude_tue_fri: bool) -> pd.DataFrame:
    cutoff_entry = dtime(15, 0)
    cutoff_eod = dtime(15, 15)
    target_pct = 0.8
    trades = []

    for _, day_df in work.groupby("session_date"):
        if exclude_tue_fri and day_df["weekday"].iloc[0] in {"Tuesday", "Friday"}:
            continue
        idxs = day_df.index.tolist()
        if len(idxs) < 6:
            continue

        in_trade = False
        trade = None
        armed_dir = None
        ref_sl = None
        cross_bar_num = None

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
                elif t_now >= cutoff_eod or bar_num == len(idxs) - 1:
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
                    cross_bar_num = None

            if (
                not in_trade
                and pd.notna(prev["sma2"])
                and pd.notna(prev["sma10"])
                and pd.notna(bar["sma2"])
                and pd.notna(bar["sma10"])
            ):
                up_cross = (prev["sma2"] <= prev["sma10"]) and (bar["sma2"] > bar["sma10"])
                dn_cross = (prev["sma2"] >= prev["sma10"]) and (bar["sma2"] < bar["sma10"])
                if up_cross and float(bar["low"]) < float(bar["sma10"]):
                    armed_dir = "CALL"
                    ref_sl = float(bar["low"])
                    cross_bar_num = bar_num
                    continue
                if dn_cross and float(bar["high"]) > float(bar["sma10"]):
                    armed_dir = "PUT"
                    ref_sl = float(bar["high"])
                    cross_bar_num = bar_num
                    continue

            if (
                not in_trade
                and armed_dir is not None
                and cross_bar_num is not None
                and (bar_num - cross_bar_num) >= 4
                and t_now <= cutoff_entry
            ):
                entry_px = float(bar["close"])
                upper_wick = float(bar["high"]) - max(float(bar["open"]), entry_px)
                lower_wick = min(float(bar["open"]), entry_px) - float(bar["low"])
                sma10_val = float(bar["sma10"])

                if armed_dir == "CALL":
                    no_touch = float(bar["low"]) > sma10_val
                    wick_ok = lower_wick > upper_wick
                    risk = entry_px - ref_sl
                    tgt_px = entry_px + (target_pct * risk)
                else:
                    no_touch = float(bar["high"]) < sma10_val
                    wick_ok = upper_wick > lower_wick
                    risk = ref_sl - entry_px
                    tgt_px = entry_px - (target_pct * risk)

                if wick_ok and no_touch and risk > 0:
                    in_trade = True
                    trade = {
                        "weekday": bar["weekday"],
                        "direction": armed_dir,
                        "entry": entry_px,
                        "sl": ref_sl,
                        "target": tgt_px,
                        "risk": risk,
                    }

    return pd.DataFrame(trades)


def drawdown_stats(tdf: pd.DataFrame) -> dict:
    if tdf.empty:
        return {"max_dd_pts": 0.0, "max_dd_r": 0.0, "max_consec_losses": 0}
    cum_pts = tdf["pnl_pts"].cumsum()
    dd_pts = float((cum_pts - cum_pts.cummax()).min())
    cum_r = tdf["r_val"].cumsum()
    dd_r = float((cum_r - cum_r.cummax()).min())

    streak = 0
    max_streak = 0
    for pnl in tdf["pnl_pts"]:
        if pnl <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "max_dd_pts": round(dd_pts, 1),
        "max_dd_r": round(dd_r, 2),
        "max_consec_losses": max_streak,
    }


def main():
    work = _prep_10m()
    print(f"Loaded 10m session candles: {len(work)} rows")

    # 1. Parameter grid on 10m
    grid_rows = []
    for max_tr in [1, 2]:
        for color_req in [True, False]:
            for tgt in TARGET_LEVELS:
                tdf = run_new_10m_trades(work, max_tr, color_req, tgt)
                m = _metrics(tdf)
                grid_rows.append({"max_trades": max_tr, "color_req": color_req, "target_pct": int(tgt * 100), **m})
    print("\n=== 1. 10m PARAMETER GRID ===")
    print(pd.DataFrame(grid_rows).to_string(index=False))

    # 2. Direction & Session splits on 10m (max_trades=2, color_req=True, target=60%)
    tdf_10m = run_new_10m_trades(work, max_trades_per_day=2, require_color=True, target_pct=0.6)
    for col in ["direction", "session"]:
        rows = []
        for val, grp in tdf_10m.groupby(col):
            rows.append({col: val, **_metrics(grp)})
        print(f"\n=== 2. 10m BREAKDOWN BY {col.upper()} (max_trades=2, color_req=True, target=60%) ===")
        print(pd.DataFrame(rows).to_string(index=False))

    # 3. Drawdown & Exit Reasons on 10m
    for max_tr in [1, 2]:
        sub = run_new_10m_trades(work, max_tr, True, 0.6)
        dd = drawdown_stats(sub)
        print(f"\n=== 3. 10m DRAWDOWN & EXITS (max_trades={max_tr}, color_req=True, target=60%) ===")
        print({**_metrics(sub), **dd})
        if not sub.empty:
            print("Exits:", sub["exit_reason"].value_counts().to_dict())

    # 4. Previous strategy rules on 10m
    for excl in [True, False]:
        prev_df = run_prev_10m_trades(work, exclude_tue_fri=excl)
        tag = "Mon/Wed/Thu Only" if excl else "All Weekdays"
        print(f"\n=== 4. PREVIOUS STRATEGY ON 10m ({tag}) ===")
        print({**_metrics(prev_df), **drawdown_stats(prev_df)})


if __name__ == "__main__":
    main()

