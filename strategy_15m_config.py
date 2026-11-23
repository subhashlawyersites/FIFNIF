from datetime import time as dtime

STRATEGY_15M_CONFIG = {
    "name": "NIFTY50_15M_SMA2_SMA10_COLOR70_MON_WED_THU",
    "interval": "15",
    "sma_fast": 2,
    "sma_slow": 10,
    "entry_earliest": dtime(9, 45),
    "entry_cutoff": dtime(15, 14),
    "eod_exit": dtime(15, 15),
    "max_trades_per_day": 2,
    "require_color": True,
    "target_pct": 0.70,          # 70% of 1R safe-exit target
    "gap_down_skip_pct": -0.0075, # Skip session if open gap <= -0.75%
    "skip_weekdays": ["Tuesday", "Friday"],  # Trade Mon, Wed, Thu; skip Tue expiry & Fri
    "reference_stats": {
        "net_pts": 1852.6,
        "net_r": 16.46,
        "win_rate_pct": 62.50,
        "max_dd_r": -6.36,
        "max_consec_losses": 4,
    },
}
