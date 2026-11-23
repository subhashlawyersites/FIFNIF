import json
import os
import re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
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


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/auth")
def auth_page():
    return send_from_directory(BASE_DIR, "fyers_auth.html")


@app.get("/api/fyers/config")
def api_fyers_config():
    load_dotenv(ENV_PATH, override=False)
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
    load_dotenv(ENV_PATH, override=False)
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or os.getenv("FYERS_CLIENT_ID") or "").strip()
    secret_key = (
        payload.get("secret_key")
        or os.getenv("FYERS_SECRET_KEY")
        or os.getenv("FYERS_APP_SECRET")
        or ""
    ).strip()
    redirect_uri = (
        payload.get("redirect_uri")
        or os.getenv("FYERS_REDIRECT_URI")
        or "https://trade.fyers.in/api-login/redirect-uri/index.html"
    ).strip()
    auth_code = (payload.get("auth_code") or "").strip()

    if not client_id or not secret_key or not auth_code:
        return jsonify({"ok": False, "error": "Missing client_id, secret_key, or auth_code"}), 400

    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()
    access_token = resp.get("access_token")
    if not access_token:
        return jsonify({"ok": False, "error": resp.get("message") or str(resp)}), 400

    _update_env_keys({
        "FYERS_CLIENT_ID": client_id,
        "FYERS_SECRET_KEY": secret_key,
        "FYERS_REDIRECT_URI": redirect_uri,
        "FYERS_ACCESS_TOKEN": access_token,
    })
    return jsonify({"ok": True, "access_token": access_token})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
