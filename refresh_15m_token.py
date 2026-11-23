import os
from urllib.parse import urlparse, parse_qs
from datetime import date, timedelta
import pandas as pd
from dotenv import load_dotenv, set_key
from fyers_apiv3 import fyersModel
from live_engine_15m import SYMBOL, IST
from mfe_and_targets_15m import analyze_15m_targets_and_mfe

env_file = ".env"
load_dotenv(dotenv_path=env_file, override=True)

client_id = os.environ["FYERS_CLIENT_ID"].strip()
secret_key = os.environ["FYERS_SECRET_KEY"].strip()
redirect_uri = os.environ["FYERS_REDIRECT_URI"].strip()

session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type="code",
    grant_type="authorization_code",
)

print("\n1. Open this URL in your browser and log in to Fyers:\n")
print(session.generate_authcode())
raw = input("\n2. Paste full redirect URL or auth_code and press Enter: ").strip()

if "auth_code=" in raw:
    auth_code = parse_qs(urlparse(raw).query).get("auth_code", [""])[0].strip()
else:
    auth_code = raw

session.set_token(auth_code)
resp = session.generate_token()
print("Token response:", resp)

if resp.get("s") != "ok" or not resp.get("access_token"):
    raise RuntimeError(f"Token generation failed: {resp}")

tok = resp["access_token"].strip()
full_token = tok if ":" in tok else f"{client_id}:{tok}"
set_key(env_file, "FYERS_ACCESS_TOKEN", full_token)

client = fyersModel.FyersModel(client_id=client_id, token=full_token, log_path="")
prof = client.get_profile()
print("Profile status:", prof.get("s"), prof.get("data", {}).get("name", prof.get("message")))

cache_path = r"nifty_cache\nifty50_15m.csv"
df_existing = pd.read_csv(cache_path)
ts_col = "timestamp" if "timestamp" in df_existing.columns else "datetime"
df_existing["timestamp"] = pd.to_datetime(df_existing[ts_col])
if df_existing["timestamp"].dt.tz is None:
    df_existing["timestamp"] = df_existing["timestamp"].dt.tz_localize(IST)
else:
    df_existing["timestamp"] = df_existing["timestamp"].dt.tz_convert(IST)

if "epoch" not in df_existing.columns:
    df_existing["epoch"] = (df_existing["timestamp"].astype("int64") // 10**9).astype("int64")

chunks = []
cur = date(2025, 11, 23)
end_gap = date(2026, 2, 19)

while cur <= end_gap:
    nxt = min(cur + timedelta(days=89), end_gap)
    hist_resp = client.history({
        "symbol": SYMBOL,
        "resolution": "15",
        "date_format": "1",
        "range_from": cur.strftime("%Y-%m-%d"),
        "range_to": nxt.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    })
    if hist_resp.get("s") == "ok" and hist_resp.get("candles"):
        cdf = pd.DataFrame(hist_resp["candles"], columns=["epoch", "open", "high", "low", "close", "volume"])
        cdf["timestamp"] = pd.to_datetime(cdf["epoch"], unit="s", utc=True).dt.tz_convert(IST)
        chunks.append(cdf)
        print(f"Fetched {cur} -> {nxt}: {len(cdf)} bars")
    else:
        print(f"Failed {cur} -> {nxt}: {hist_resp}")
    cur = nxt + timedelta(days=1)

if chunks:
    df_new = pd.concat(chunks, ignore_index=True)
    merged = (
        pd.concat([df_existing, df_new], ignore_index=True)
        .drop_duplicates(subset=["epoch"])
        .sort_values("epoch")
        .reset_index(drop=True)
    )
    merged.to_csv(cache_path, index=False)
    print(f"Updated cache: {merged['timestamp'].min()} -> {merged['timestamp'].max()} ({len(merged)} bars)")
    analyze_15m_targets_and_mfe(max_trades_per_day=2, require_color=True, target_pct=1.00, df_15m=merged)
