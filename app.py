import json
import os
import re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from flask import flash, Flask, jsonify, render_template, request, send_from_directory, redirect, url_for
from fyers_apiv3 import fyersModel

from live_engine_15m import IST, build_fyers_client, fetch_recent_15m_candles
from strategy_15m_service import (
    build_analytics_payload,
    build_monitor_payload,
    evaluate_15m_snapshot,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
TRADES_PATH = BASE_DIR / "nifty_cache" / "manual_trades_15m.json"

load_dotenv(ENV_PATH)

app = Flask(__name__)


def _load_manual_trades() -> list[dict]:
    if not TRADES_PATH.exists():
        return []
    try:
        return json.loads(TRADES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_manual_trades(trades: list[dict]) -> None:
    TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADES_PATH.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def _update_env_keys(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            new_lines.append(f"{key}={updates[key]}")
            os.environ[key] = updates[key]
            seen.add(key)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}")
            os.environ[key] = val
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _exchange_fyers_auth_code(
    auth_code: str,
    client_id: str = "",
    secret_key: str = "",
    redirect_uri: str = "",
) -> tuple[bool, str]:
    load_dotenv(ENV_PATH, override=True)
    default_redirect = "https://trade.fyers.in/api-login/redirect-uri/index.html"
    cid = (client_id or os.getenv("FYERS_CLIENT_ID") or "").strip()
    sec = (secret_key or os.getenv("FYERS_SECRET_KEY") or os.getenv("FYERS_APP_SECRET") or "").strip()
    redir = (redirect_uri or os.getenv("FYERS_REDIRECT_URI") or default_redirect).strip()
    code = (auth_code or "").strip()

    if not cid or not sec or not code:
        return False, "Missing client_id, secret_key, or auth_code"

    session = fyersModel.SessionModel(
        client_id=cid,
        secret_key=sec,
        redirect_uri=redir,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(code)
    resp = session.generate_token()
    access_token = (resp or {}).get("access_token")
    if not access_token:
        return False, (resp or {}).get("message") or str(resp)

    updates = {
        "FYERS_CLIENT_ID": cid,
        "FYERS_SECRET_KEY": sec,
        "FYERS_REDIRECT_URI": redir,
        "FYERS_ACCESS_TOKEN": access_token,
    }
    _update_env_keys(updates)
    os.environ.update(updates)
    save_fyers_token_to_db((os.getenv("FYERS_ACCESS_TOKEN") or "").strip())
    try:
        flash("Connected Live with Fyers", "success")
    except Exception:
        pass
    return True, access_token


@app.get("/")
def dashboard():
    incoming_code = (request.args.get("auth_code") or "").strip()
    if incoming_code:
        ok, _ = _exchange_fyers_auth_code(incoming_code)
        if ok:
            return redirect(url_for("dashboard", auth_ok=1))
    return render_template("dashboard.html")


@app.get("/auth")
def auth_page():
    return send_from_directory(BASE_DIR, "fyers_auth.html")


def _extract_auth_code(raw_val: str) -> str:
    val = (raw_val or "").strip()
    if not val:
        return ""
    if "auth_code=" in val:
        parsed = urlparse(val if "://" in val else ("https://example.com/?" + val.lstrip("?")))
        codes = parse_qs(parsed.query).get("auth_code") or []
        if codes:
            return codes[0].strip()
    return val


@app.post("/api/fyers/exchange")
def api_fyers_exchange():
    data = request.get_json(silent=True) or {}
    code = _extract_auth_code(data.get("auth_code") or data.get("redirect_url") or "")
    ok, result = _exchange_fyers_auth_code(
        code,
        client_id=(data.get("client_id") or "").strip(),
        secret_key=(data.get("secret_key") or "").strip(),
        redirect_uri=(data.get("redirect_uri") or "").strip(),
    )
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "message": "Fyers Authentication successful"})


# --- SUPABASE_FYERS_TOKEN_STORE_START ---
def _get_token_db_url() -> str:
    for k in ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return ""


def _ensure_token_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fyers_runtime_tokens (
                id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def save_fyers_token_to_db(token: str) -> bool:
    tok = (token or "").strip()
    db_url = _get_token_db_url()
    if not db_url:
        return False
    try:
        import psycopg
        with psycopg.connect(db_url, autocommit=True) as conn:
            _ensure_token_table(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fyers_runtime_tokens (id, access_token, updated_at)
                    VALUES ('default', %s, NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET access_token = EXCLUDED.access_token, updated_at = NOW()
                """, (tok,))
        return True
    except Exception:
        pass
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            _ensure_token_table(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fyers_runtime_tokens (id, access_token, updated_at)
                    VALUES ('default', %s, NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET access_token = EXCLUDED.access_token, updated_at = NOW()
                """, (tok,))
        finally:
            conn.close()
        return True
    except Exception:
        return False


def load_fyers_token_from_db() -> str:
    db_url = _get_token_db_url()
    if not db_url:
        return ""
    try:
        import psycopg
        with psycopg.connect(db_url, autocommit=True) as conn:
            _ensure_token_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT access_token FROM fyers_runtime_tokens WHERE id = 'default'")
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
    except Exception:
        pass
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            _ensure_token_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT access_token FROM fyers_runtime_tokens WHERE id = 'default'")
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def get_active_fyers_token() -> str:
    env_tok = (os.getenv("FYERS_ACCESS_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    db_tok = load_fyers_token_from_db()
    if db_tok:
        os.environ["FYERS_ACCESS_TOKEN"] = db_tok
    return db_tok
# --- SUPABASE_FYERS_TOKEN_STORE_END ---


@app.post("/api/fyers/disconnect")
def api_fyers_disconnect():
    os.environ["FYERS_ACCESS_TOKEN"] = ""
    save_fyers_token_to_db("")
    return jsonify({"ok": True, "connected": False, "message": "Disconnected from Live Feed"})


@app.get("/api/fyers/config")
def api_fyers_config():
    load_dotenv(ENV_PATH, override=True)
    default_redirect = "https://trade.fyers.in/api-login/redirect-uri/index.html"
    client_id = (os.getenv("FYERS_CLIENT_ID") or "").strip()
    secret_key = (os.getenv("FYERS_SECRET_KEY") or os.getenv("FYERS_APP_SECRET") or "").strip()
    redirect_uri = (os.getenv("FYERS_REDIRECT_URI") or default_redirect).strip()
    has_token = bool((os.getenv("FYERS_ACCESS_TOKEN") or "").strip())
    return jsonify({
        "ok": True,
        "client_id": client_id,
        "secret_key": secret_key,
        "redirect_uri": redirect_uri,
        "has_token": has_token,
    })


@app.get("/api/strategy/status")
def api_strategy_status():
    try:
        days_back = int(request.args.get("days", 10))
        snap = evaluate_15m_snapshot(days_back=days_back, persist_state=True)
        return jsonify({"ok": True, **snap})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/monitor/status")
def api_monitor_status():
    try:
        snap = evaluate_15m_snapshot(days_back=10, persist_state=True)
        manual_open = [t for t in _load_manual_trades() if t.get("status") == "OPEN"]
        mon = build_monitor_payload(snap, actual_trades=manual_open)
        return jsonify({"ok": True, **mon})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/journal/analytics")
def api_journal_analytics():
    try:
        days_back = int(request.args.get("days", 365))
        mode_filter = (request.args.get("mode") or "ALL").upper()
        client = build_fyers_client()
        candles = fetch_recent_15m_candles(client, days_back=days_back)

        manual_all = _load_manual_trades()
        manual_filtered = (
            manual_all
            if mode_filter == "ALL"
            else [t for t in manual_all if str(t.get("mode", "")).upper() == mode_filter]
        )

        ana = build_analytics_payload(candles, manual_trades=manual_filtered)
        ana["mode_filter"] = mode_filter
        ana["manual_trades"] = manual_filtered[-50:][::-1]
        return jsonify({"ok": True, **ana})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/trades")
def api_get_trades():
    mode_filter = (request.args.get("mode") or "ALL").upper()
    trades = _load_manual_trades()
    if mode_filter != "ALL":
        trades = [t for t in trades if str(t.get("mode", "")).upper() == mode_filter]
    return jsonify({"ok": True, "trades": trades[::-1]})


@app.post("/api/trades")
def api_add_trade():
    payload = request.get_json(silent=True) or {}
    trades = _load_manual_trades()
    trade_id = int(datetime.now(IST).timestamp() * 1000)
    entry_px = float(payload.get("entry_price") or 0)
    exit_px = float(payload.get("exit_price") or 0)
    qty = int(payload.get("qty") or 75)
    charges = float(payload.get("charges") or 0)
    status = "CLOSED" if exit_px > 0 else "OPEN"
    gross = round((exit_px - entry_px) * qty, 2) if status == "CLOSED" else 0.0
    net = round(gross - charges, 2) if status == "CLOSED" else 0.0

    record = {
        "id": trade_id,
        "date": payload.get("date") or datetime.now(IST).strftime("%Y-%m-%d"),
        "mode": (payload.get("mode") or "PAPER").upper(),
        "direction": (payload.get("direction") or "PUT").upper(),
        "symbol": (payload.get("symbol") or "").strip() or "NIFTY OPTION",
        "qty": qty,
        "entry_price": entry_px,
        "exit_price": exit_px if exit_px > 0 else None,
        "charges": charges,
        "gross_pnl": gross,
        "net_pnl": net,
        "status": status,
        "notes": (payload.get("notes") or "").strip(),
    }
    trades.append(record)
    _save_manual_trades(trades)
    return jsonify({"ok": True, "trade": record})


@app.post("/api/fyers/token")
def api_fyers_token():
    payload = request.get_json(silent=True) or {}
    ok, result = _exchange_fyers_auth_code(
        auth_code=str(payload.get("auth_code") or ""),
        client_id=str(payload.get("client_id") or ""),
        secret_key=str(payload.get("secret_key") or payload.get("secret") or ""),
        redirect_uri=str(payload.get("redirect_uri") or ""),
    )
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "access_token": result})




def _load_fallback_15m_df(days_back: int = 90):
    import pandas as pd
    cache_candidates = [
        BASE_DIR / "nifty_cache" / "nifty50_15m.csv",
        BASE_DIR / "nifty_cache" / "nifty_15m.csv",
    ]
    for p in cache_candidates:
        if p.exists():
            try:
                df = pd.read_csv(p)
                if "timestamp" in df.columns and not df.empty:
                    df["timestamp"] = pd.to_datetime(df["timestamp"])
                    return df.tail(days_back * 26).reset_index(drop=True)
            except Exception:
                pass
    return None



@app.get("/api/15m/alerts")
def api_get_15m_alerts():
    from supabase_15m_store import fetch_recent_15m_alerts
    limit = max(1, min(100, int(request.args.get("limit", 25) or 25)))
    items = fetch_recent_15m_alerts(limit=limit)
    unread = sum(1 for x in items if not x.get("is_read"))
    return jsonify({"ok": True, "items": items, "unread_count": unread})


@app.post("/api/15m/alerts")
def api_post_15m_alert():
    from supabase_15m_store import classify_weekday_regime, emit_15m_alert
    p = request.get_json(silent=True) or {}
    weekday = str(p.get("weekday") or datetime.now(IST).strftime("%A"))
    eligible, skip_reason = classify_weekday_regime(weekday)
    event_type = str(p.get("event_type") or "signal_entry")
    if not eligible and event_type in ("signal_entry", "target_70_hit"):
        event_type = "skip_weekday"

    session_date = str(p.get("session_date") or datetime.now(IST).strftime("%Y-%m-%d"))
    bar_time = str(p.get("bar_time") or datetime.now(IST).strftime("%H:%M"))
    direction = str(p.get("direction") or "CALL").upper()
    title = str(p.get("title") or f"15m {direction} {event_type.replace('_', ' ').upper()}")
    msg = str(
        p.get("message")
        or (
            f"{session_date} ({weekday}, {skip_reason}) {bar_time} {direction} | "
            f"Entry: {p.get('entry_price', '-')} | SL: {p.get('sl_price', '-')} | 0.70R Target: {p.get('target_70_price', '-')}"
        )
    )
    res = emit_15m_alert(
        session_date=session_date,
        bar_time=bar_time,
        direction=direction,
        event_type=event_type,
        title=title,
        message=msg,
        target_url=str(p.get("target_url") or "/15m-edge"),
        priority="HIGH" if event_type in ("signal_entry", "target_70_hit", "stop_loss") else "INFO",
    )
    return jsonify(res)



from datetime import datetime as _dt_now_cls
from zoneinfo import ZoneInfo as _ZoneInfo_cls

_IST_TZ = _ZoneInfo_cls("Asia/Kolkata")

def _parse_ist_dt(val):
    if val is None or val == "":
        return None
    try:
        if hasattr(val, "tzinfo"):
            dt = val.to_pydatetime() if hasattr(val, "to_pydatetime") else val
            if dt.tzinfo is None:
                return dt.replace(tzinfo=_IST_TZ)
            return dt.astimezone(_IST_TZ)
        s = str(val).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt_now_cls.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_IST_TZ)
        return dt.astimezone(_IST_TZ)
    except Exception:
        return None

def _compute_15m_freshness_ist(candles_df=None, trades=None, stale_after_minutes=20):
    now_ist = _dt_now_cls.now(_IST_TZ)
    latest_dt = None
    latest_src = "none"

    if candles_df is not None and getattr(candles_df, "empty", True) is False:
        idx = getattr(candles_df, "index", None)
        if idx is not None and len(idx) > 0:
            latest_dt = _parse_ist_dt(idx[-1])
            if latest_dt is not None:
                latest_src = "candle_index"
        if latest_dt is None:
            for col in ("timestamp", "datetime", "date", "time"):
                if col in getattr(candles_df, "columns", []):
                    latest_dt = _parse_ist_dt(candles_df[col].iloc[-1])
                    if latest_dt is not None:
                        latest_src = f"candle_{col}"
                        break

    if latest_dt is None and trades:
        for tr in trades:
            for key in ("exit_time", "entry_time", "session_date"):
                latest_dt = _parse_ist_dt(tr.get(key))
                if latest_dt is not None:
                    latest_src = f"trade_{key}"
                    break
            if latest_dt is not None:
                break

    if latest_dt is None:
        return {
            "timezone": "IST",
            "now_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
            "last_bar_ist": None,
            "age_minutes": None,
            "is_market_hours_ist": False,
            "is_stale": True,
            "status": "NO_DATA",
            "source": latest_src,
        }

    age_minutes = max(0, int((now_ist - latest_dt).total_seconds() // 60))
    hm_now = now_ist.hour * 60 + now_ist.minute
    is_mkt_hours = (now_ist.weekday() < 5) and ((9 * 60 + 15) <= hm_now <= (15 * 60 + 30))
    is_stale = bool(is_mkt_hours and age_minutes > stale_after_minutes)

    if is_stale:
        status = "STALE"
    elif is_mkt_hours:
        status = "LIVE_FRESH"
    else:
        status = "OFF_HOURS"

    return {
        "timezone": "IST",
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "last_bar_ist": latest_dt.strftime("%Y-%m-%d %H:%M IST"),
        "last_bar_hm": latest_dt.strftime("%H:%M IST"),
        "age_minutes": age_minutes,
        "stale_after_minutes": stale_after_minutes,
        "is_market_hours_ist": is_mkt_hours,
        "is_stale": is_stale,
        "status": status,
        "source": latest_src,
    }

@app.get("/api/15m/analytics")
def api_15m_analytics():
    days_back = max(5, min(365, int(request.args.get("days_back", 90) or 90)))
    candles_df = None
    source_mode = "unavailable"
    detail_msg = "No 15m candle data available yet."

    try:
        client = build_fyers_client()
        if client is not None:
            candles_df = fetch_recent_15m_candles(client, days_back=days_back)
            if candles_df is not None and not candles_df.empty:
                source_mode = "fyers_live_15m"
                detail_msg = f"Loaded {len(candles_df)} 15m bars ({days_back}d window)."
    except Exception as exc:
        detail_msg = f"Live fetch fallback: {exc}"

    if candles_df is None or candles_df.empty:
        fb_df = _load_fallback_15m_df(days_back=days_back)
        if fb_df is not None and not fb_df.empty:
            candles_df = fb_df
            source_mode = "local_cache_15m"
            detail_msg = f"Loaded {len(candles_df)} cached 15m bars."

    if candles_df is None or candles_df.empty:
        return jsonify({
            "ok": True,
            "source_mode": source_mode,
            "message": detail_msg,
            "trades": [],
            "paper_summary": {},
            "series": {},
        })

    from strategy_15m_service import EngineState, run_engine_on_closed_bars, split_closed_and_preview_bars
    payload = build_analytics_payload(candles_df, manual_trades=_load_manual_trades())
    closed_df, _ = split_closed_and_preview_bars(candles_df)
    _, engine_events = run_engine_on_closed_bars(closed_df, EngineState(), persist_path=None)
    all_closed_trades = [
        e["trade"] for e in engine_events
        if str(e.get("event", "")).startswith("EXIT_") and e.get("trade")
    ]
    raw_trades = all_closed_trades[::-1] if all_closed_trades else (payload.get("recent_15m_trades") or [])
    try:
        from supabase_15m_store import sync_15m_trades_to_supabase
        if all_closed_trades:
            sync_15m_trades_to_supabase(all_closed_trades)
    except Exception:
        pass
    active_days = {"Monday", "Wednesday", "Thursday"}

    normalized_trades = []
    for idx, tr in enumerate(raw_trades, 1):
        wk = str(tr.get("weekday") or "")
        pts = float(tr.get("pnl_pts", 0.0) or 0.0)
        entry_raw = str(tr.get("entry_time") or "")
        exit_raw = str(tr.get("exit_time") or "")
        entry_hm = entry_raw[11:16] if len(entry_raw) >= 16 else entry_raw
        exit_hm = exit_raw[11:16] if len(exit_raw) >= 16 else exit_raw
        outcome = "WIN" if pts > 0 else ("LOSS" if pts < 0 else "FLAT")
        normalized_trades.append({
            "id": tr.get("id") or f"15m_{idx}_{tr.get('date', '')}_{entry_hm}",
            "session_date": str(tr.get("date") or ""),
            "weekday": wk,
            "is_eligible_day": wk in active_days,
            "direction": str(tr.get("direction") or ""),
            "crossover_time": str(tr.get("cross_time") or entry_hm),
            "entry_time": entry_hm,
            "exit_time": exit_hm,
            "exit_reason": str(tr.get("exit_reason") or ""),
            "mfe": float(tr.get("mfe_pts", 0.0) or 0.0),
            "mae": float(tr.get("mae_pts", 0.0) or 0.0),
            "points": round(pts, 2),
            "r_val": round(float(tr.get("r_val", 0.0) or 0.0), 2),
            "outcome": outcome,
        })

    freshness = _compute_15m_freshness_ist(candles_df=candles_df, trades=normalized_trades, stale_after_minutes=20)
    return jsonify({
        "ok": True,
        "source_mode": source_mode,
        "message": detail_msg,
        "freshness": freshness,
        "trades": normalized_trades,
        "paper_summary": payload.get("paper_summary") or {},
        "series": payload.get("series") or {},
    })


@app.get("/15m-edge")
def edge_15m_analytics():
    return render_template("analytics_15m.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
