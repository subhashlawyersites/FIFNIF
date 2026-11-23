from datetime import time as dtime
import pandas as pd
from pull_and_test import load_or_fetch_candles


def backtest_previous_rules(df: pd.DataFrame, tf_label: str, exclude_tue_fri: bool = False):
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

    cutoff_entry = dtime(15, 0)
    cutoff_eod = dtime(15, 15)
    target_pct = 0.8
    trades = []

    for s_date, day_df in work.groupby("session_date"):
        if s_date in excluded_dates:
            continue
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

            # 1. Manage active trade
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

            # 2. Check new SMA2/SMA10 crossover + reference candle SMA10 touch rule
            if (
                not in_trade
                and pd.notna(prev["sma2"])
                and pd.notna(prev["sma10"])
                and pd.notna(bar["sma2"])
                and pd.notna(bar["sma10"])
            ):
                up_cross = (prev["sma2"] <= prev["sma10"]) and (bar["sma2"] > bar["sma10"])
                dn_cross = (prev["sma2"] >= prev["sma10"]) and (bar["sma2"] < bar["sma10"])

                # Reference candle is bar (the crossover candle) or prev; check bar first
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

            # 3. Entry after waiting at least 4 full candles, candle completes <= 15:00 IST, color ignored
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

    tdf = pd.DataFrame(trades)
    label_days = "Mon/Wed/Thu Only" if exclude_tue_fri else "All Weekdays"
    print(f"\n================ PREVIOUS STRATEGY ({tf_label} | {label_days}) ================")
    if tdf.empty:
        print("No trades triggered.")
        return

    wins = int((tdf["pnl_pts"] > 0).sum())
    print(
        f"Total Trades: {len(tdf)} | Win Rate: {round(100.0 * wins / len(tdf), 2)}% | "
        f"Net Pts: {round(float(tdf['pnl_pts'].sum()), 1)} | "
        f"Net R: {round(float(tdf['r_val'].sum()), 2)} | "
        f"Avg R: {round(float(tdf['r_val'].mean()), 3)}"
    )

    for col in ["direction", "weekday"]:
        grp = tdf.groupby(col, as_index=False).agg(
            trades=("pnl_pts", "count"),
            win_rate_pct=("pnl_pts", lambda s: round(100.0 * (s > 0).mean(), 2)),
            net_pts=("pnl_pts", lambda s: round(float(s.sum()), 1)),
            net_r=("r_val", lambda s: round(float(s.sum()), 2)),
            avg_r=("r_val", lambda s: round(float(s.mean()), 3)),
        )
        print(f"\n--- Breakdown by {col} ---")
        print(grp.to_string(index=False))


if __name__ == "__main__":
    df10 = load_or_fetch_candles(None, "10")
    backtest_previous_rules(df10, "10m", exclude_tue_fri=True)
    backtest_previous_rules(df10, "10m", exclude_tue_fri=False)

    df15 = load_or_fetch_candles(None, "15")
    backtest_previous_rules(df15, "15m", exclude_tue_fri=False)
