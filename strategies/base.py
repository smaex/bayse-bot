import logging
from dataclasses import dataclass, field
from typing import Optional, List
from abc import ABC, abstractmethod
from collections import deque

log = logging.getLogger("strategies")


@dataclass
class MarketState:
    price_history:         dict = field(default_factory=dict)   # asset → deque[(time, price)]
    kalman_state:          dict = field(default_factory=dict)   # asset → {x, P, last_time}
    garch_state:           dict = field(default_factory=dict)   # asset → {var, last_price}
    last_history_update:   dict = field(default_factory=dict)
    circuit_breakers:      dict = field(default_factory=dict)
    systemic_halt_until:   float = 0.0
    # CORRELATE
    btc_signal_time:       dict = field(default_factory=dict)
    btc_signal_direction:  dict = field(default_factory=dict)
    btc_signal_move:       dict = field(default_factory=dict)
    # Market state tracking
    market_flips:          dict = field(default_factory=dict)
    market_last_fav:       dict = field(default_factory=dict)
    market_opening_prices: dict = field(default_factory=dict)


global_state = MarketState()


@dataclass
class TradeSignal:
    strategy:     str
    event_id:     str
    market_id:    str
    asset:        str
    timeframe:    str
    outcome:      str           # "YES" | "NO" | "ARB"
    outcome_id:   str
    certainty:    float         # composite 0–1
    win_prob:     float         # raw win probability (for Kelly)
    market_price: float         # current AMM price
    size_pct:     float         # fraction of bankroll
    reason:       str
    title:        str = ""
    converged_with: list = field(default_factory=list)
    # Quant snapshot at entry
    momentum_at_entry:     float = 0.0
    regime_at_entry:       float = 0.0
    edge_at_entry:         float = 0.0
    realized_vol_at_entry: float = 0.0
    mode_floor:            float = 0.48

    def strength(self) -> str:
        if self.certainty >= 0.85: return "🔥 SUPERIOR"
        if self.certainty >= 0.70: return "⚡ STRONG"
        if self.certainty >= 0.55: return "⚖️ BALANCED"
        return "🛡️ CAUTIOUS"


class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name
        self.log  = logging.getLogger(f"strat.{name.lower()}")

    @abstractmethod
    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        pass
