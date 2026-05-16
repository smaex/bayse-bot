import logging
from dataclasses import dataclass, field
from typing import Optional, Literal, List
from abc import ABC, abstractmethod
from collections import deque

log = logging.getLogger("strategies")

@dataclass
class MarketState:
    price_history:        dict[str, deque] = field(default_factory=dict)
    kalman_state:         dict[str, dict]  = field(default_factory=dict)
    garch_state:          dict[str, dict]  = field(default_factory=dict)
    last_history_update:  dict[str, float] = field(default_factory=dict)
    circuit_breakers:     dict[str, dict]  = field(default_factory=dict)
    systemic_halt_until:  float            = 0.0
    # For CORRELATE
    btc_signal_time:      dict[str, float] = field(default_factory=dict)
    btc_signal_direction: dict[str, str]   = field(default_factory=dict)
    btc_signal_move:      dict[str, float] = field(default_factory=dict)

global_state = MarketState()

StrategyType = Literal["SNIPE", "CORRELATE", "ARB", "NEWS", "POLY_EDGE"]

@dataclass
class TradeSignal:
    strategy: StrategyType
    event_id: str
    market_id: str
    asset: str
    timeframe: str
    outcome: str          # "YES" or "NO"
    outcome_id: str
    certainty: float      # composite 0–1
    win_prob: float       # raw estimated win probability (for Kelly)
    market_price: float   # current AMM price of chosen outcome
    size_pct: float       # fraction of bankroll (quarter-Kelly)
    reason: str
    title: str = ""
    arb_quantity: float = 0.0
    converged_with: list = field(default_factory=list)
    
    # ML feature snapshot at entry time
    momentum_at_entry:     float = 0.0
    regime_at_entry:       float = 0.0
    edge_at_entry:         float = 0.0
    realized_vol_at_entry: float = 0.0
    mode_floor:            float = 0.55
    
    def strength(self) -> str:
        if self.certainty >= 0.85: return "🔥 SUPERIOR"
        if self.certainty >= 0.70: return "⚡ STRONG"
        if self.certainty >= 0.55: return "⚖️ BALANCED"
        return "🛡️ CAUTIOUS"

class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name
        self.log = logging.getLogger(f"strat.{name.lower()}")

    @abstractmethod
    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        """
        Evaluate a single market and return a TradeSignal if an edge is found.
        """
        pass

    def __repr__(self):
        return f"<Strategy:{self.name}>"
