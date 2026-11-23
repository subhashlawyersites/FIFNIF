from datetime import time
import pandas as pd


def categorize_session(ts_series: pd.Series) -> pd.Series:
    """Split IST timestamps into Morning (09:15-11:59) and Afternoon (12:00-15:30)."""
    t = ts_series.dt.time
    return pd.Series(
        [
            "Morning (09:15-11:59)" if time(9, 15) <= x < time(12, 0) else "Afternoon (12:00-15:30)"
            for x in t
        ],
        index=ts_series.index,
    )


def summarize_trades(trades_df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    grouped = trades_df.groupby(group_col, dropna=False)
    summary = grouped.agg(
        trades=("net_pts", "count"),
        win_rate_pct=("net_pts", lambda s: round((s > 0).mean() * 100, 2)),
        net_pts=("net_pts", "sum"),
        net_r=("r_mult", "sum"),
        avg_r=("r_mult", "mean"),
    ).reset_index()
    summary["net_pts"] = summary["net_pts"].round(1)
    summary["net_r"] = summary["net_r"].round(2)
    summary["avg_r"] = summary["avg_r"].round(3)
    return summary


if __name__ == "__main__":
    df = pd.read_csv("nifty_cache/nifty50_15m.csv")
    df["ts"] = pd.to_datetime(df["timestamp"])
    df["session"] = categorize_session(df["ts"])
    print("Loaded rows:", len(df))
    print(df["session"].value_counts())