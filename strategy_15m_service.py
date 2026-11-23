from datetime import timedelta
from typing import Optional

import pandas as pd

from engine_15m import EngineState
from live_engine_15m import (
    STATE_PATH,
    build_fyers_client,
    fetch_recent_15m_candles,
    run_engine_on_closed_bars,
    split_closed_and_preview_bars,
)
from strategy_15m_config import STRATEGY_15M_CONFIG


def _compute_progress(trade: dict, current_px: float) -> dict:
    direction = trade.get("direction")
    entry = float(trade["entry"])
    sl = float(trade["sl"])
    tgt = float(trade["target"])

    if direction == "CALL":
        tgt_span = tgt - entry
        sl_span = entry - sl
        tgt_pct = ((current_px - entry) / tgt_span * 100.0) if tgt_span > 0 else 0.0
        sl_pct = ((entry - current_px) / sl_span * 100.0) if sl_span > 0 else 0.0
        levels = {str(p): round(entry + (p / 100.0) * tgt_span, 2) for p in (60, 70, 80, 90, 100)}
        sl_levels = {str(p): round(entry - (p / 100.0) * sl_span, 2) for p in (60, 80, 100)}
    else:
        tgt_span = entry - tgt
        sl_span = sl - entry
        tgt_pct = ((entry - current_px) / tgt_span * 100.0) if tgt_span > 0 else 0.0
        sl_pct = ((current_px - entry) / sl_span * 100.0) if sl_span > 0 else 0.0
        levels = {str(p): round(entry - (p / 100.0) * tgt_span, 2) for p in (60, 70, 80, 90, 100)}
        sl_levels = {str(p): round(entry + (p / 100.0) * sl_span, 2) for p in (60, 80, 100)}

    tgt_clamped = round(max(0.0, min(100.0, tgt_pct)), 1)
    sl_clamped = round(max(0.0, min(100.0, sl_pct)), 1)

    triggered = []
    for p in (60, 70, 80, 90, 100):
        if tgt_clamped >= p:
            triggered.append(f"TARGET_{p}%")
    for p in (60, 80, 100):
        if sl_clamped >= p:
            triggered.append(f"SL_LOSS_{p}%")

    return {
        "target_progress_pct": tgt_clamped,
        "loss_progress_pct": sl_clamped,
        "target_levels": levels,
        "loss_levels": sl_levels,
        "triggered_alerts": triggered,
    }


def evaluate_15m_snapshot(
    candles_df: Optional[pd.DataFrame] = None,
    days_back: int = 10,
    persist_state: bool = True,
) -> dict:
    if candles_df is None:
        client = build_fyers_client()
        candles_df = fetch_recent_15m_candles(client, days_back=days_back)

    closed_df, preview_bar = split_closed_and_preview_bars(candles_df)
    state, events = run_engine_on_closed_bars(
        closed_df,
        EngineState(),
        STATE_PATH if persist_state else None,
    )

    work = closed_df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)
    work["sma2"] = work["close"].rolling(STRATEGY_15M_CONFIG["sma_fast"]).mean()
    work["sma10"] = work["close"].rolling(STRATEGY_15M_CONFIG["sma_slow"]).mean()

    latest_candle = None
    if not work.empty:
        last_row = work.iloc[-1]
        latest_candle = {
            "end_time": (last_row["timestamp"] + timedelta(minutes=15)).strftime("%H:%M"),
            "open": round(float(last_row["open"]), 2),
            "high": round(float(last_row["high"]), 2),
            "low": round(float(last_row["low"]), 2),
            "close": round(float(last_row["close"]), 2),
            "sma2": None if pd.isna(last_row["sma2"]) else round(float(last_row["sma2"]), 2),
            "sma10": None if pd.isna(last_row["sma10"]) else round(float(last_row["sma10"]), 2),
        }

    session_events = [e for e in events if str(e.get("timestamp", "")).startswith(state.session_date or "")]
    today_entries = [e["trade"] for e in session_events if e.get("event") == "ENTRY_SIGNAL" and e.get("trade")]
    today_exits = [e["trade"] for e in session_events if str(e.get("event", "")).startswith("EXIT_") and e.get("trade")]

    if state.in_trade and state.active_trade:
        status = "SIGNAL_ACTIVE"
        message = f"Active {state.active_trade['direction']} @ {state.active_trade['entry']} | SL {state.active_trade['sl']} | Tgt(0.6R) {state.active_trade['target']}"
    elif state.gap_excluded:
        status = "SESSION_SKIPPED"
        message = "Session excluded by weekday or <= -0.75% gap-down rule"
    elif state.armed_dir:
        status = "SETUP_ARMED"
        message = f"Armed {state.armed_dir} | Ref SL {state.ref_sl} | Waiting for entry bar filter"
    else:
        status = "WAITING"
        message = f"Flat ? Trades today: {state.trades_today}/{STRATEGY_15M_CONFIG['max_trades_per_day']}"

    return {
        "strategy": STRATEGY_15M_CONFIG["name"],
        "timeframe": "15m",
        "target_pct": STRATEGY_15M_CONFIG["target_pct"],
        "status": status,
        "message": message,
        "session_date": state.session_date,
        "completed_candles": int(len(work)),
        "latest_candle": latest_candle,
        "preview_bar": preview_bar,
        "state": state.to_dict(),
        "active_trade": state.active_trade,
        "today_entries": today_entries,
        "today_exits": today_exits,
        "recent_events": session_events[-10:],
    }


def build_monitor_payload(snapshot: dict, actual_trades: Optional[list[dict]] = None) -> dict:
    latest = snapshot.get("latest_candle") or {}
    preview = snapshot.get("preview_bar") or {}
    current_nifty = preview.get("close") if preview.get("close") is not None else latest.get("close")

    strategy_positions = []
    if snapshot.get("active_trade") and current_nifty is not None:
        tr = snapshot["active_trade"]
        strategy_positions.append({
            "label": f"15m {tr['direction']} #{tr['trade_no']}",
            "source": "15M_ENGINE",
            "status": "OPEN",
            "nifty_entry": tr["entry"],
            "nifty_sl": tr["sl"],
            "nifty_target": tr["target"],
            "option_symbol": "NSE:NIFTY50-INDEX",
            "option_current_premium": None,
            "progress": _compute_progress(tr, float(current_nifty)),
        })

    return {
        "current_nifty": current_nifty,
        "feed_stale": False,
        "itm_quotes": {},
        "actual_positions": actual_trades or [],
        "strategy_positions": strategy_positions,
    }


def _period_key(date_str: str, grain: str) -> str:
    dt = pd.to_datetime(date_str, errors="coerce")
    if pd.isna(dt):
        return date_str
    if grain == "daily":
        return dt.strftime("%Y-%m-%d")
    if grain == "weekly":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if grain == "monthly":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y")


def _aggregate_series(
    closed_trades: list[dict],
    manual_trades: Optional[list[dict]],
    grain: str,
) -> list[dict]:
    buckets: dict[str, dict] = {}

    def ensure_bucket(k: str) -> dict:
        if k not in buckets:
            buckets[k] = {
                "label": k,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "calls": 0,
                "puts": 0,
                "paper_points": 0.0,
                "paper_r": 0.0,
                "actual_gross": 0.0,
                "actual_charges": 0.0,
                "actual_net": 0.0,
                "paper_trades_count": 0,
                "real_trades_count": 0,
            }
        return buckets[k]

    for t in closed_trades:
        d_str = str(t.get("date", ""))
        if not d_str:
            continue
        b = ensure_bucket(_period_key(d_str, grain))
        pts = float(t.get("pnl_pts", 0.0))
        r_val = float(t.get("r_val", 0.0))
        b["trades"] += 1
        b["paper_points"] = round(b["paper_points"] + pts, 2)
        b["paper_r"] = round(b["paper_r"] + r_val, 2)
        if pts > 0:
            b["wins"] += 1
        elif pts < 0:
            b["losses"] += 1
        if t.get("direction") == "CALL":
            b["calls"] += 1
        elif t.get("direction") == "PUT":
            b["puts"] += 1

    for m in (manual_trades or []):
        if m.get("status") != "CLOSED":
            continue
        d_str = str(m.get("date", ""))
        if not d_str:
            continue
        b = ensure_bucket(_period_key(d_str, grain))
        gross = float(m.get("gross_pnl", 0.0))
        charges = float(m.get("charges", 0.0))
        net = float(m.get("net_pnl", gross - charges))
        b["actual_gross"] = round(b["actual_gross"] + gross, 2)
        b["actual_charges"] = round(b["actual_charges"] + charges, 2)
        b["actual_net"] = round(b["actual_net"] + net, 2)
        if str(m.get("mode", "")).upper() == "REAL":
            b["real_trades_count"] += 1
        else:
            b["paper_trades_count"] += 1

    cum_pts = 0.0
    cum_r = 0.0
    cum_net = 0.0
    peak_pts = 0.0
    max_dd_pts = 0.0
    out = []

    for k in sorted(buckets.keys()):
        b = buckets[k]
        cum_pts = round(cum_pts + b["paper_points"], 2)
        cum_r = round(cum_r + b["paper_r"], 2)
        cum_net = round(cum_net + b["actual_net"], 2)
        if cum_pts > peak_pts:
            peak_pts = cum_pts
        dd = round(peak_pts - cum_pts, 2)
        if dd > max_dd_pts:
            max_dd_pts = dd

        win_rate = round((b["wins"] / b["trades"] * 100.0) if b["trades"] else 0.0, 1)
        b["win_rate"] = win_rate
        b["cum_paper_points"] = cum_pts
        b["cum_paper_r"] = cum_r
        b["cum_actual_net"] = cum_net
        b["max_dd_points"] = max_dd_pts
        out.append(b)

    return out


def build_analytics_payload(
    candles_df: pd.DataFrame,
    manual_trades: Optional[list[dict]] = None,
) -> dict:
    closed_df, _ = split_closed_and_preview_bars(candles_df)
    _, events = run_engine_on_closed_bars(closed_df, EngineState(), persist_path=None)

    closed_trades = [e["trade"] for e in events if str(e.get("event", "")).startswith("EXIT_") and e.get("trade")]
    wins = [t for t in closed_trades if float(t.get("pnl_pts", 0.0)) > 0]
    losses = [t for t in closed_trades if float(t.get("pnl_pts", 0.0)) < 0]

    net_points = round(sum(float(t.get("pnl_pts", 0.0)) for t in closed_trades), 2)
    net_r = round(sum(float(t.get("r_val", 0.0)) for t in closed_trades), 2)
    win_rate = round((len(wins) / len(closed_trades) * 100.0) if closed_trades else 0.0, 1)
    avg_win = round((sum(float(t["pnl_pts"]) for t in wins) / len(wins)) if wins else 0.0, 2)
    avg_loss = round((sum(float(t["pnl_pts"]) for t in losses) / len(losses)) if losses else 0.0, 2)

    calls = sum(1 for t in closed_trades if t.get("direction") == "CALL")
    puts = sum(1 for t in closed_trades if t.get("direction") == "PUT")

    series_by_grain = {
        g: _aggregate_series(closed_trades, manual_trades, g)
        for g in ("daily", "weekly", "monthly", "yearly")
    }

    daily_rows = series_by_grain["daily"]
    best_day = max(daily_rows, key=lambda x: x["paper_points"]) if daily_rows else None
    worst_day = min(daily_rows, key=lambda x: x["paper_points"]) if daily_rows else None
    max_dd_pts = max((r["max_dd_points"] for r in daily_rows), default=0.0)

    closed_manual = [t for t in (manual_trades or []) if t.get("status") == "CLOSED"]
    paper_manual = [t for t in closed_manual if str(t.get("mode", "")).upper() != "REAL"]
    real_manual = [t for t in closed_manual if str(t.get("mode", "")).upper() == "REAL"]

    gross_inr = round(sum(float(t.get("gross_pnl", 0.0)) for t in closed_manual), 2)
    charges_inr = round(sum(float(t.get("charges", 0.0)) for t in closed_manual), 2)
    net_inr = round(sum(float(t.get("net_pnl", 0.0)) for t in closed_manual), 2)
    paper_net_inr = round(sum(float(t.get("net_pnl", 0.0)) for t in paper_manual), 2)
    real_net_inr = round(sum(float(t.get("net_pnl", 0.0)) for t in real_manual), 2)

    entry_count = sum(1 for e in events if e.get("event") == "ENTRY_SIGNAL")
    open_count = max(0, entry_count - len(closed_trades))

    return {
        "total_signals": len(closed_trades),
        "paper_summary": {
            "net_points": net_points,
            "net_r": net_r,
            "win_rate": win_rate,
            "wins": len(wins),
            "losses": len(losses),
            "open_trades": open_count,
            "breakeven": 0,
            "avg_win_pts": avg_win,
            "avg_loss_pts": avg_loss,
            "best_day": best_day,
            "worst_day": worst_day,
            "max_dd_points": max_dd_pts,
            "direction": {"CALL": calls, "PUT": puts},
            "side": {"LONG": calls, "SHORT": puts},
        },
        "actual_summary": {
            "net_pnl": net_inr,
            "gross_pnl": gross_inr,
            "total_charges": charges_inr,
            "paper_net_pnl": paper_net_inr,
            "real_net_pnl": real_net_inr,
            "closed_count": len(closed_manual),
            "paper_count": len(paper_manual),
            "real_count": len(real_manual),
        },
        "series": series_by_grain,
        "recent_15m_trades": closed_trades[-20:][::-1],
    }
