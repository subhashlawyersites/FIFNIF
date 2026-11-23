from dataclasses import dataclass, asdict, fields
from datetime import datetime, time as dtime
from typing import Optional


@dataclass
class EngineState:
    session_date: Optional[str] = None
    gap_excluded: bool = False
    bar_num: int = 0
    trades_today: int = 0
    armed_dir: Optional[str] = None  # "CALL" | "PUT" | None
    ref_sl: Optional[float] = None
    in_trade: bool = False
    active_trade: Optional[dict] = None
    prev_close: Optional[float] = None
    prev_low: Optional[float] = None
    prev_high: Optional[float] = None
    prev_sma2: Optional[float] = None
    prev_sma10: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EngineState":
        valid_names = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in (data or {}).items() if k in valid_names}
        return cls(**filtered)


def step_15m_engine(
    state: EngineState,
    bar: dict,
    sma2: Optional[float],
    sma10: Optional[float],
    *,
    max_trades_per_day: int = 2,
    require_color: bool = True,
    target_pct: float = 0.60,
    gap_down_skip_pct: float = -0.0075,
    skip_weekdays: tuple[str, ...] = ("Friday",),
    entry_earliest: dtime = dtime(9, 45),
    entry_latest: dtime = dtime(15, 14),
    eod_exit_time: dtime = dtime(15, 15),
) -> list[dict]:
    ts = bar["timestamp"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)

    s_date = ts.date().isoformat()
    weekday_name = ts.strftime("%A")
    t_now = ts.time()

    o = float(bar["open"])
    h = float(bar["high"])
    l = float(bar["low"])
    c = float(bar["close"])
    events: list[dict] = []

    if state.session_date != s_date:
        if state.in_trade and state.active_trade is not None:
            events.append({
                "event": "EXIT_EOD",
                "timestamp": ts.isoformat(),
                "exit_price": state.prev_close if state.prev_close is not None else c,
                "trade": state.active_trade,
            })
        state.session_date = s_date
        state.bar_num = 0
        state.trades_today = 0
        state.armed_dir = None
        state.ref_sl = None
        state.in_trade = False
        state.active_trade = None

        if weekday_name in skip_weekdays:
            state.gap_excluded = True
            events.append({"event": "SKIP_WEEKDAY", "timestamp": ts.isoformat(), "weekday": weekday_name})
        elif state.prev_close is not None and state.prev_close > 0:
            gap_pct = (o - state.prev_close) / state.prev_close
            state.gap_excluded = gap_pct <= gap_down_skip_pct
            if state.gap_excluded:
                events.append({"event": "SKIP_GAP_DOWN", "timestamp": ts.isoformat(), "gap_pct": round(gap_pct * 100, 3)})
        else:
            state.gap_excluded = False

    state.bar_num += 1
    if state.gap_excluded:
        state.prev_close = c
        state.prev_low = l
        state.prev_high = h
        state.prev_sma2 = sma2
        state.prev_sma10 = sma10
        return events

    if state.in_trade and state.active_trade is not None:
        tr = state.active_trade
        d = tr["direction"]
        sl = tr["sl"]
        tgt = tr["target"]

        hit_sl = (l <= sl) if d == "CALL" else (h >= sl)
        hit_tgt = (h >= tgt) if d == "CALL" else (l <= tgt)

        exit_reason = None
        exit_px = None
        if hit_sl and hit_tgt:
            exit_reason, exit_px = "EXIT_SL_AMBIGUOUS", sl
        elif hit_sl:
            exit_reason, exit_px = "EXIT_SL", sl
        elif hit_tgt:
            exit_reason, exit_px = "EXIT_TARGET", tgt
        elif t_now >= eod_exit_time:
            exit_reason, exit_px = "EXIT_EOD_1515", c

        if exit_reason is not None:
            pts = (exit_px - tr["entry"]) if d == "CALL" else (tr["entry"] - exit_px)
            r_val = (pts / tr["risk"]) if tr["risk"] > 0 else 0.0
            closed = {
                **tr,
                "exit_time": ts.isoformat(),
                "exit_price": round(exit_px, 2),
                "exit_reason": exit_reason,
                "pnl_pts": round(pts, 2),
                "r_val": round(r_val, 3),
            }
            events.append({"event": exit_reason, "timestamp": ts.isoformat(), "trade": closed})
            state.in_trade = False
            state.active_trade = None
            state.armed_dir = None
            state.ref_sl = None

    armed_this_bar = False
    if (
        not state.in_trade
        and sma2 is not None
        and sma10 is not None
        and state.prev_sma2 is not None
        and state.prev_sma10 is not None
    ):
        up_cross = (state.prev_sma2 <= state.prev_sma10) and (sma2 > sma10)
        dn_cross = (state.prev_sma2 >= state.prev_sma10) and (sma2 < sma10)
        if up_cross and state.prev_close is not None and state.prev_low is not None:
            state.armed_dir = "CALL"
            state.ref_sl = float(bar["prev_low_override"]) if "prev_low_override" in bar else float(state.prev_low)
            armed_this_bar = True
            events.append({
                "event": "ARMED_CALL",
                "timestamp": ts.isoformat(),
                "ref_sl": state.ref_sl,
                "sma2": round(sma2, 2),
                "sma10": round(sma10, 2),
            })
        elif dn_cross and state.prev_close is not None and state.prev_high is not None:
            state.armed_dir = "PUT"
            state.ref_sl = float(bar["prev_high_override"]) if "prev_high_override" in bar else float(state.prev_high)
            armed_this_bar = True
            events.append({
                "event": "ARMED_PUT",
                "timestamp": ts.isoformat(),
                "ref_sl": state.ref_sl,
                "sma2": round(sma2, 2),
                "sma10": round(sma10, 2),
            })

    if (
        not state.in_trade
        and not armed_this_bar
        and state.armed_dir is not None
        and state.ref_sl is not None
        and sma10 is not None
        and state.trades_today < max_trades_per_day
        and state.bar_num >= 4
        and entry_earliest <= t_now <= entry_latest
    ):
        entry_px = c
        upper_wick = h - max(o, entry_px)
        lower_wick = min(o, entry_px) - l

        if state.armed_dir == "CALL":
            color_ok = (entry_px > o) if require_color else True
            no_touch = l > sma10
            wick_ok = lower_wick > upper_wick
            risk = entry_px - state.ref_sl
            tgt_px = entry_px + (target_pct * risk)
        else:
            color_ok = (entry_px < o) if require_color else True
            no_touch = h < sma10
            wick_ok = upper_wick > lower_wick
            risk = state.ref_sl - entry_px
            tgt_px = entry_px - (target_pct * risk)

        if color_ok and no_touch and wick_ok and risk > 0:
            state.in_trade = True
            state.trades_today += 1
            trade_obj = {
                "trade_no": state.trades_today,
                "date": s_date,
                "weekday": weekday_name,
                "entry_time": ts.isoformat(),
                "direction": state.armed_dir,
                "entry": round(entry_px, 2),
                "sl": round(state.ref_sl, 2),
                "target": round(tgt_px, 2),
                "risk": round(risk, 2),
                "risk_pct": round((risk / entry_px) * 100.0, 3),
            }
            state.active_trade = trade_obj
            events.append({"event": "ENTRY_SIGNAL", "timestamp": ts.isoformat(), "trade": trade_obj})

    state.prev_close = c
    state.prev_low = l
    state.prev_high = h
    state.prev_sma2 = sma2
    state.prev_sma10 = sma10
    return events
