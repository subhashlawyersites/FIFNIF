# NIFTY 50 ? 15-Minute SMA 2 / SMA 10 Trade Manual

**Project Folder:** `C:\NiftyCrossoverBacktest`  
**Strategy ID:** `NIFTY50_15M_SMA2_SMA10_COLOR60`  
**Role:** Decision-support, paper tracking, and signal verification (no unapproved automated live order placement).

---

## 1. Core Strategy Parameters (`strategy_15m_config.py`)

- **Instrument:** `NSE:NIFTY50-INDEX`
- **Candle Timeframe:** `15` minutes (`09:15` to `15:30` IST)
- **Indicators:**
  - `SMA 2` on `close` (`rolling(2).mean()`)
  - `SMA 10` on `close` (`rolling(10).mean()`)
- **Continuous SMAs vs. Session State:**
  - `SMA 2` and `SMA 10` are computed continuously across sessions so morning bars start with valid prior-close moving averages.
  - Intraday state (`bar_num`, `trades_today`, `armed_dir`, `ref_sl`, `in_trade`, `active_trade`, `gap_excluded`) resets on each new trading day (`session_date`).
- **Active Trading Days:** Monday through Thursday (`skip_weekdays = ["Friday"]` during validation).
- **Opening Gap Filter:** If `(first_bar_open - prev_session_close) / prev_session_close <= -0.0075` (`<= -0.75%`), entries are skipped for that session.
- **Warm-Up & Entry Window:**
  - `bar_num >= 3` / entry evaluated from 4th session bar onward (`entry_earliest = 09:45` IST through `entry_latest = 15:14` IST).
  - No new entries at or after `15:15` IST.
- **Daily Trade Cap:** Maximum `2` filled trades per session (`trades_today < 2`).
- **Candle Colour Requirement:** `require_color = True`.
- **Target:** `0.60` (`60%` of `1R`).
- **EOD Exit:** `15:15` IST (`eod_exit_time = 15:15`).

---

## 2. Signal Arming & Entry Rules (`engine_15m.py`)

### A. Crossover Arming (On Closed 15m Bar Only)
- **Bullish Arm (`CALL`):** `prev_sma2 <= prev_sma10` and `sma2 > sma10`  
  Locks `armed_dir = "CALL"` and `ref_sl = float(prev_low)`.
- **Bearish Arm (`PUT`):** `prev_sma2 >= prev_sma10` and `sma2 < sma10`  
  Locks `armed_dir = "PUT"` and `ref_sl = float(prev_high)`.
- If an opposite crossover occurs while flat and armed, `armed_dir` and `ref_sl` update to that new crossover's prior bar extreme.

### B. Entry Confirmation (Evaluated at Candle Close `entry = close`)
For **CALL** (`armed_dir == "CALL"`):
1. Green candle: `close > open`
2. Above `SMA 10`: `low > sma10`
3. Lower wick greater than upper wick: `(min(open, close) - low) > (high - max(open, close))`
4. Positive risk: `risk = entry - ref_sl > 0`  
   Target: `target = entry + 0.60 * risk`

For **PUT** (`armed_dir == "PUT"`):
1. Red candle: `close < open`
2. Below `SMA 10`: `high < sma10`
3. Upper wick greater than lower wick: `(high - max(open, close)) > (min(open, close) - low)`
4. Positive risk: `risk = ref_sl - entry > 0`  
   Target: `target = entry - 0.60 * risk`

---

## 3. Exit Rules & Risk Discipline

- **Fixed Stop-Loss:** Never widen `ref_sl` mid-trade.
- **Conservative Same-Bar Rule:** If both `SL` and `target` are breached on the same `15m` candle, record `EXIT_SL_AMBIGUOUS` at `sl` first.
- **Target Exit:** `high >= target` (`CALL`) or `low <= target` (`PUT`) -> `EXIT_TARGET` at `target`.
- **Stop-Loss Exit:** `low <= ref_sl` (`CALL`) or `high >= ref_sl` (`PUT`) -> `EXIT_SL` at `sl`.
- **Session Close Exit:** At `time >= 15:15` IST -> `EXIT_EOD_1515` at `close`.
- **Backtest Reference Guardrail:** Budget position sizing around historical drawdowns (`~4.58 R` Mon?Thu / `~6.36 R` Mon?Fri, up to 3?4 consecutive losses) plus option spreads, slippage, and charges. Index `R` is not identical to option premium `%` return.

---

## 4. Live vs. Replay Safety Rules

- **Closed Candles Only (`BAR_CLOSED`):** Never confirm signals or change engine state on an incomplete/forming candle (`LIVE_PREVIEW`).
- **State Persistence:** Persist `EngineState.to_dict()` across restarts and reload via `EngineState.from_dict(...)` so `prev_close`, `prev_low`, `prev_high`, `prev_sma2`, `prev_sma10`, `armed_dir`, `ref_sl`, and `trades_today` stay consistent.
