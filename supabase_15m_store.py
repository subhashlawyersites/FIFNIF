import os
from typing import Any
import requests

ACTIVE_WEEKDAYS = {"Monday", "Wednesday", "Thursday"}
TARGET_PCT_15M = 0.70


def _supabase_headers() -> tuple[str, dict[str, str]] | None:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    if not url or not key:
        return None
    return (
        f"{url}/rest/v1",
        {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )


def classify_weekday_regime(weekday: str) -> tuple[bool, str]:
    wd = (weekday or "").strip()
    if wd in ACTIVE_WEEKDAYS:
        return True, "NONE"
    if wd == "Tuesday":
        return False, "TUESDAY_EXPIRY"
    if wd == "Friday":
        return False, "FRIDAY_FILTER"
    return False, "NON_ACTIVE_DAY"


def upsert_15m_signals_all_days(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = _supabase_headers()
    if not cfg or not rows:
        return {"ok": False, "reason": "supabase_not_configured_or_empty", "count": 0}

    base_url, headers = cfg
    headers = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}

    payload_rows = []
    for r in rows:
        wd = str(r.get("weekday") or "")
        eligible, skip_reason = classify_weekday_regime(wd)
        payload_rows.append({
            "session_date": str(r.get("session_date") or r.get("date") or ""),
            "weekday": wd,
            "bar_time": str(r.get("entry_time") or r.get("bar_time") or ""),
            "direction": str(r.get("direction") or "CALL").upper(),
            "entry_price": float(r.get("entry_price") or 0.0),
            "sl_price": float(r.get("sl_price") or 0.0),
            "target_70_price": float(r.get("target_70_price") or 0.0),
            "risk_pts": float(r.get("risk_pts") or 0.0),
            "target_pct": TARGET_PCT_15M,
            "is_eligible_day": eligible,
            "skip_reason": skip_reason,
            "status": str(r.get("status") or "CLOSED"),
            "exit_price": r.get("exit_price"),
            "exit_time": r.get("exit_time"),
            "pnl_pts": float(r.get("points") if r.get("points") is not None else (r.get("pnl_pts") or 0.0)),
            "pnl_r": float(r.get("r_val") if r.get("r_val") is not None else (r.get("pnl_r") or 0.0)),
            "mfe_pts": float(r.get("mfe") if r.get("mfe") is not None else (r.get("mfe_pts") or 0.0)),
            "mae_pts": float(r.get("mae") if r.get("mae") is not None else (r.get("mae_pts") or 0.0)),
        })

    resp = requests.post(
        f"{base_url}/nifty_15m_signals?on_conflict=session_date,bar_time,direction",
        headers=headers,
        json=payload_rows,
        timeout=15,
    )
    return {"ok": resp.status_code in (200, 201, 204), "status": resp.status_code, "count": len(payload_rows)}


def emit_15m_alert(
    session_date: str,
    bar_time: str,
    direction: str,
    event_type: str,
    title: str,
    message: str,
    target_url: str = "/15m-edge",
    priority: str = "HIGH",
) -> dict[str, Any]:
    cfg = _supabase_headers()
    alert_key = f"{session_date}_{bar_time}_{direction.upper()}_{event_type}"
    if not cfg:
        return {"ok": False, "alert_key": alert_key, "reason": "supabase_not_configured"}

    base_url, headers = cfg
    headers = {**headers, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    row = {
        "alert_key": alert_key,
        "event_type": event_type,
        "priority": priority,
        "title": title,
        "message": message,
        "target_url": target_url,
        "is_read": False,
    }
    resp = requests.post(
        f"{base_url}/nifty_15m_alerts?on_conflict=alert_key",
        headers=headers,
        json=row,
        timeout=10,
    )
    return {"ok": resp.status_code in (200, 201, 204), "status": resp.status_code, "alert_key": alert_key}


def fetch_recent_15m_alerts(limit: int = 25) -> list[dict[str, Any]]:
    cfg = _supabase_headers()
    if not cfg:
        return []
    base_url, headers = cfg
    resp = requests.get(
        f"{base_url}/nifty_15m_alerts?select=*&order=created_at.desc&limit={max(1, min(100, limit))}",
        headers=headers,
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json() or []
    return []


def _fmt_dt_hm(val: Any) -> str:
    s = str(val or "").strip()
    if len(s) >= 16 and ("T" in s or " " in s):
        return s[11:16]
    return s[:5] if len(s) >= 5 else s


def sync_15m_trades_to_supabase(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for tr in trades or []:
        dt_str = str(tr.get("date") or tr.get("session_date") or "")[:10]
        wd = str(tr.get("weekday") or "")
        entry_px = float(tr.get("entry_px") if tr.get("entry_px") is not None else (tr.get("entry_price") or 0.0))
        sl_px = float(tr.get("sl_px") if tr.get("sl_px") is not None else (tr.get("sl_price") or 0.0))
        risk_pts = float(tr.get("risk_pts") or abs(entry_px - sl_px))
        direction = str(tr.get("direction") or "CALL").upper()
        tgt_70 = tr.get("target_70_price")
        if tgt_70 is None:
            tgt_70 = round(entry_px + 0.70 * risk_pts, 2) if direction == "CALL" else round(entry_px - 0.70 * risk_pts, 2)
        exit_reason = str(tr.get("exit_reason") or "").upper()
        r_val = float(tr.get("r_val") if tr.get("r_val") is not None else (tr.get("pnl_r") or 0.0))
        if exit_reason == "EXIT_TARGET" or r_val >= 0.699:
            status = "TARGET_70_HIT"
        elif exit_reason in ("EXIT_SL", "EXIT_SL_AMBIGUOUS"):
            status = "SL_HIT"
        elif exit_reason:
            status = "EOD_1515"
        else:
            status = "OPEN"
        rows.append({
            "session_date": dt_str,
            "weekday": wd,
            "bar_time": _fmt_dt_hm(tr.get("entry_time") or tr.get("bar_time")),
            "direction": direction,
            "entry_price": round(entry_px, 2),
            "sl_price": round(sl_px, 2),
            "target_70_price": round(float(tgt_70 or 0.0), 2),
            "risk_pts": round(risk_pts, 2),
            "status": status,
            "exit_price": tr.get("exit_px") if tr.get("exit_px") is not None else tr.get("exit_price"),
            "exit_time": _fmt_dt_hm(tr.get("exit_time")),
            "pnl_pts": tr.get("pnl_pts") if tr.get("pnl_pts") is not None else tr.get("points", 0.0),
            "pnl_r": r_val,
            "mfe_pts": tr.get("mfe_pts") if tr.get("mfe_pts") is not None else tr.get("mfe", 0.0),
            "mae_pts": tr.get("mae_pts") if tr.get("mae_pts") is not None else tr.get("mae", 0.0),
        })
    return upsert_15m_signals_all_days(rows)


def dispatch_15m_engine_alerts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for ev in events or []:
        ev_name = str(ev.get("event") or "").upper()
        tr = ev.get("trade") or {}
        dt_str = str(tr.get("date") or tr.get("session_date") or ev.get("date") or "")[:10]
        wd = str(tr.get("weekday") or ev.get("weekday") or "")
        eligible, skip_reason = classify_weekday_regime(wd)
        if not eligible:
            continue
        bar_hm = _fmt_dt_hm(ev.get("time") or tr.get("entry_time") or tr.get("exit_time"))
        direction = str(tr.get("direction") or ev.get("direction") or "CALL").upper()
        entry_px = tr.get("entry_px", tr.get("entry_price", "-"))
        sl_px = tr.get("sl_px", tr.get("sl_price", "-"))
        risk_pts = tr.get("risk_pts")
        tgt_70 = tr.get("target_70_price")
        if tgt_70 is None and isinstance(entry_px, (int, float)) and isinstance(risk_pts, (int, float)):
            tgt_70 = round(float(entry_px) + 0.70 * float(risk_pts), 2) if direction == "CALL" else round(float(entry_px) - 0.70 * float(risk_pts), 2)

        if ev_name in ("ENTRY", "SIGNAL_ENTRY"):
            results.append(emit_15m_alert(
                session_date=dt_str,
                bar_time=bar_hm,
                direction=direction,
                event_type="signal_entry",
                title=f"15m {direction} ENTRY ({wd})",
                message=f"{dt_str} {bar_hm} {direction} | Entry: {entry_px} | SL: {sl_px} | 0.70R Target: {tgt_70}",
            ))
        elif ev_name in ("EXIT_TARGET", "TARGET_70_HIT"):
            results.append(emit_15m_alert(
                session_date=dt_str,
                bar_time=bar_hm,
                direction=direction,
                event_type="target_70_hit",
                title=f"15m {direction} 0.70R SAFE EXIT",
                message=f"{dt_str} {bar_hm} {direction} hit 0.70R target ({tgt_70}) | PnL: {tr.get('pnl_pts', '-')} pts ({tr.get('r_val', '-')}R)",
            ))
        elif ev_name in ("EXIT_SL", "EXIT_SL_AMBIGUOUS", "STOP_LOSS"):
            results.append(emit_15m_alert(
                session_date=dt_str,
                bar_time=bar_hm,
                direction=direction,
                event_type="stop_loss",
                title=f"15m {direction} STOP LOSS",
                message=f"{dt_str} {bar_hm} {direction} hit SL ({sl_px}) | PnL: {tr.get('pnl_pts', '-')} pts ({tr.get('r_val', '-')}R)",
            ))
        elif ev_name.startswith("EXIT_") or ev_name == "TRADE_EXIT":
            results.append(emit_15m_alert(
                session_date=dt_str,
                bar_time=bar_hm,
                direction=direction,
                event_type="trade_exit",
                title=f"15m {direction} EXIT ({ev_name})",
                message=f"{dt_str} {bar_hm} {direction} exited ({ev_name}) | PnL: {tr.get('pnl_pts', '-')} pts ({tr.get('r_val', '-')}R)",
                priority="INFO",
            ))
    return results
