import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=True)

client_id = (os.getenv("FYERS_CLIENT_ID") or "").strip()
redirect_uri = (os.getenv("FYERS_REDIRECT_URI") or "").strip()

secret_key = input("Enter FYERS Secret Key: ").strip()
login_url = (
    f"https://api-t1.fyers.in/api/v3/generate-authcode"
    f"?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&state=backtest"
)
print("\n1. Open this exact URL in your browser and log in:\n")
print(login_url)
print("\n2. After login, copy the FULL URL from the browser address bar (even if the page shows a message).")

pasted = input("\nPaste full redirected URL or auth_code here: ").strip()
auth_code = pasted
if "auth_code=" in pasted:
    parsed = urlparse(pasted)
    qs = parse_qs(parsed.query)
    if "auth_code" in qs:
        auth_code = qs["auth_code"][0]
    else:
        m = re.search(r"auth_code=([^&]+)", pasted)
        if m:
            auth_code = m.group(1)

session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type="code",
    grant_type="authorization_code",
)
session.set_token(auth_code)
resp = session.generate_token()

token = resp.get("access_token")
if not token:
    raise SystemExit(f"Token generation failed: {resp}")

lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
updated = False
new_lines = []
for line in lines:
    if line.startswith("FYERS_ACCESS_TOKEN="):
        new_lines.append(f"FYERS_ACCESS_TOKEN={token}")
        updated = True
    else:
        new_lines.append(line)
if not updated:
    new_lines.append(f"FYERS_ACCESS_TOKEN={token}")

ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
print("\nSuccess! Fresh FYERS_ACCESS_TOKEN saved to .env.")