from datetime import time as dtime

STRATEGY_15M_CONFIG = {
    "name": "NIFTY50_15M_SMA2_SMA10_COLOR60",
    "interval": "15",
    "sma_fast": 2,
    "sma_slow": 10,
    "entry_earliest": dtime(9, 45),
    "entry_cutoff": dtime(15, 14),
    "eod_exit": dtime(15, 15),
    "max_trades_per_day": 2,
    "require_color": True,
    "target_pct": 0.60,          # 60% of 1R
    "gap_down_skip_pct": -0.0075, # Skip session if open gap <= -0.75%
    "skip_weekdays": ["Friday"],  # Optional paper-test hypothesis; set [] to trade all weekdays
    "reference_stats": {
        "net_pts": 1972.6,
        "net_r": 16.70,
        "win_rate_pct": 64.74,
        "max_dd_r": -6.36,
        "max_consec_losses": 4,
    },
}
