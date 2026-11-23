import json
import os
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

from engine_15m import EngineState, step_15m_engine
from strategy_15m_config import STRATEGY_15M_CONFIG

IST = timezone(timedelta(hours=5, minutes=30))
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "nifty_cache" / "engine_15m_state.json"
SYMBOL = "NSE:NIFTY50-INDEX"


def build_fyers_client() -> fyersModel.FyersModel:
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    client_id = (os.getenv("FYERS_CLIENT_ID") or "").strip().strip('"').strip("'")
    token = (os.getenv("FYERS_ACCESS_TOKEN") or "").strip().strip('"').strip("'")
    if not client_id or not token:
        raise RuntimeError("Missing FYERS_CLIENT_ID or FYERS_ACCESS_TOKEN in .env")
    return fyersModel.FyersModel(client_id=client_id, token=token, log_path="")


def load_persisted_state(path: Path = STATE_PATH) -> EngineState:
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return EngineState.from_dict(raw)
    return EngineState()


def save_persisted_state(state: EngineState, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def _bucket_15m_start(dt_ist: datetime) -> datetime:
    floored_min = (dt_ist.minute // 15) * 15
    return dt_ist.replace(minute=floored_min, second=0, microsecond=0)


def split_closed_and_preview_bars(
    df_15m: pd.DataFrame,
    now_ist: Optional[datetime] = None,
) -> tuple[pd.DataFrame, Optional[dict]]:
    """
    Returns (closed_candles_df, preview_bar_dict_or_None).
    A 15m candle starting at T is considered closed only once now_ist >= T + 15 minutes.
    """
    if df_15m.empty:
        return df_15m.copy(), None

    if now_ist is None:
        now_ist = datetime.now(IST)
    elif now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST)
    else:
        now_ist = now_ist.astimezone(IST)

    work = df_15m.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    if work["timestamp"].dt.tz is None:
        work["timestamp"] = work["timestamp"].dt.tz_localize(IST)
    else:
        work["timestamp"] = work["timestamp"].dt.tz_convert(IST)

    work = work.sort_values("timestamp").reset_index(drop=True)
    candle_end = work["timestamp"] + timedelta(minutes=15)
    is_closed = candle_end <= now_ist

    closed_df = work[is_closed].reset_index(drop=True)
    open_df = work[~is_closed].reset_index(drop=True)

    preview_bar = None
    if not open_df.empty:
        last_open = open_df.iloc[-1]
        preview_bar = {
            "mode": "LIVE_PREVIEW",
            "timestamp": last_open["timestamp"].isoformat(),
            "open": float(last_open["open"]),
            "high": float(last_open["high"]),
            "low": float(last_open["low"]),
            "close": float(last_open["close"]),
        }
    return closed_df, preview_bar


def run_engine_on_closed_bars(
    closed_df: pd.DataFrame,
    state: Optional[EngineState] = None,
    persist_path: Optional[Path] = None,
) -> tuple[EngineState, list[dict]]:
    """
    Feeds only CLOSED 15m bars into step_15m_engine using continuous SMA2 and SMA10.
    """
    if closed_df.empty:
        return (state or EngineState()), []

    work = closed_df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)
    work["sma2"] = work["close"].rolling(STRATEGY_15M_CONFIG["sma_fast"]).mean()
    work["sma10"] = work["close"].rolling(STRATEGY_15M_CONFIG["sma_slow"]).mean()

    st = state if state is not None else EngineState()
    all_events: list[dict] = []

    for row in work.itertuples(index=False):
        s2 = None if pd.isna(row.sma2) else float(row.sma2)
        s10 = None if pd.isna(row.sma10) else float(row.sma10)
        evs = step_15m_engine(
            st,
            {
                "timestamp": row.timestamp.to_pydatetime(),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
            },
            s2,
            s10,
            max_trades_per_day=STRATEGY_15M_CONFIG["max_trades_per_day"],
            require_color=STRATEGY_15M_CONFIG["require_color"],
            target_pct=STRATEGY_15M_CONFIG["target_pct"],
            gap_down_skip_pct=STRATEGY_15M_CONFIG["gap_down_skip_pct"],
            skip_weekdays=tuple(STRATEGY_15M_CONFIG["skip_weekdays"]),
            entry_earliest=STRATEGY_15M_CONFIG["entry_earliest"],
            entry_latest=STRATEGY_15M_CONFIG["entry_cutoff"],
            eod_exit_time=STRATEGY_15M_CONFIG["eod_exit"],
        )
        for ev in evs:
            ev_with_mode = {"mode": "BAR_CLOSED", **ev}
            all_events.append(ev_with_mode)

    if persist_path is not None:
        save_persisted_state(st, persist_path)

    return st, all_events


def fetch_recent_15m_candles(
    fyers: fyersModel.FyersModel,
    days_back: int = 10,
    symbol: str = SYMBOL,
) -> pd.DataFrame:
    end_dt = datetime.now(IST).date()
    start_dt = end_dt - timedelta(days=days_back)
    resp = fyers.history({
        "symbol": symbol,
        "resolution": STRATEGY_15M_CONFIG["interval"],
        "date_format": "1",
        "range_from": start_dt.strftime("%Y-%m-%d"),
        "range_to": end_dt.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    if resp.get("s") != "ok" or not resp.get("candles"):
        raise RuntimeError(f"FYERS 15m history fetch failed: {resp}")

    df = pd.DataFrame(resp["candles"], columns=["epoch", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["epoch"]).sort_values("epoch").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert(IST)
    df = df[
        (df["timestamp"].dt.time >= dtime(9, 15))
        & (df["timestamp"].dt.time <= dtime(15, 30))
    ].reset_index(drop=True)
    return df


if __name__ == "__main__":
    client = build_fyers_client()
    candles = fetch_recent_15m_candles(client, days_back=10)
    closed_bars, preview = split_closed_and_preview_bars(candles)
    final_state, events = run_engine_on_closed_bars(closed_bars, EngineState(), STATE_PATH)

    entry_events = [e for e in events if e["event"] == "ENTRY_SIGNAL"]
    exit_events = [e for e in events if e["event"].startswith("EXIT_")]
    print(f"Closed 15m bars processed: {len(closed_bars)}")
    print(f"Preview bar present: {preview is not None} ({preview['timestamp'] if preview else 'None'})")
    print(f"Entry signals: {len(entry_events)} | Closed trades: {len(exit_events)}")
    print(f"Session: {final_state.session_date} | Trades today: {final_state.trades_today} | In trade: {final_state.in_trade}")
