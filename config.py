from dataclasses import dataclass
import os
from typing import Optional, List

@dataclass
class BotConfig:
    # Trading parameters (optimal from backtest)
    pump_threshold: float = 0.25           # 25% rise from low24
    tp_percent: float = 0.40                # TP = local_high * 0.60
    stall_bars: int = 4                      # candles without new high
    sl_multiplier: float = 4.0                # SL = entry * 4
    near_high_ratio: float = 0.88             # price >=88% of high24
    
    # Filters
    min_turnover_usdt: float = 5_000_000      # min volume 
    max_daily_gain: float = 20.0               # max daily move
    
    # Risk management
    base_risk_per_trade: float = 0.04          # base notional = 4% of USDT wallet, before multiplier
    initial_capital: float = 1000.0
    max_concurrent_trades: int = 100
    
    # Technical
    category: str = "linear"
    timeframe: str = "15"
    wake_seconds: int = 5
    cooldown_minutes: int = 60
    watch_ttl_hours: int = 24
    
    # API
    max_retries: int = 3
    retry_delays: Optional[List[int]] = None
    
    # Paths
    data_dir: str = os.getenv("DATA_DIR", "./data")
    
    # Funding
    enable_funding_guard: bool = True
    funding_guard_ratio: float = 1.0
    
    def __post_init__(self):
        if self.retry_delays is None:
            self.retry_delays = [1, 5, 15]
