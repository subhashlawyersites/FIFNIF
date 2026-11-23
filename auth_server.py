import os
import re
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from fyers_apiv3 import fyersModel

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

app = Flask(__name__)


def update_env_keys(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []

    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "fyers_auth.html")


@app.post("/api/fyers/token")
def generate_access_token():
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    secret_key = (payload.get("secret_key") or "").strip()
    redirect_uri = (payload.get("redirect_uri") or "").strip()
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

    update_env_keys({
        "FYERS_CLIENT_ID": client_id,
        "FYERS_ACCESS_TOKEN": access_token,
    })
    return jsonify({"ok": True, "access_token": access_token})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)