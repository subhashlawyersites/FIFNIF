import os
from pathlib import Path
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

client_id = (os.getenv("FYERS_CLIENT_ID") or "").strip().strip('"').strip("'")
token = (os.getenv("FYERS_ACCESS_TOKEN") or "").strip().strip('"').strip("'")

print("ENV exists:", env_path.exists())
print("Client ID:", client_id if client_id else "<MISSING>")
print("Token length:", len(token))
print("Token dot count (JWT usually has 2 dots):", token.count("."))

if not client_id or not token:
    raise SystemExit("Missing FYERS_CLIENT_ID or FYERS_ACCESS_TOKEN in .env")

fyers = fyersModel.FyersModel(client_id=client_id, token=token, log_path="")
profile = fyers.get_profile()
print("Profile response:", profile)