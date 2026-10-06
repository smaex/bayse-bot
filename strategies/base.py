import logging
from dataclasses import dataclass, field
from typing import Optional, List
from abc import ABC, abstractmethod
from collections import deque

log = logging.getLogger("strategies")


@dataclass(frozen=True)
class QuoteLeg:
    """One order belonging to a (possibly multi-leg) decision.

    A two-sided MAKER quote is *one* risk decision that happens to be two
    orders. Modelling it as two independent signals would run the risk budget
    check twice, let one leg be admitted and the other rejected, and leave the
    intended pair half-built -- which is exactly the state the pair constraint
    exists to prevent.
    """

    outcome: str
    outcome_id: str
    price: float
    size_pct: float
    fair_value: float = 0.0


@dataclass
class MarketState:
    price_history:         dict = field(default_factory=dict)   # asset → deque[(time, price)]
    kalman_state:          dict = field(default_factory=dict)   # asset → {x, P, last_time}
    garch_state:           dict = field(default_factory=dict)   # asset → {var, last_price}
    last_history_update:   dict = field(default_factory=dict)
    circuit_breakers:      dict = field(default_factory=dict)
    systemic_halt_until:   float = 0.0
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
    # The account's conviction floor for this strategy, expressed on the SAME
    # scale as ``certainty``. 0.0 means "no floor of its own": the strategy's
    # admission gates already decide, which is the right default because a
    # floor on the wrong scale is worse than no floor. A TAKER's certainty is
    # a rescaled model probability and a MAKER pair's is a raw probability,
    # so a single shared constant silently means "model probability >= 0.74"
    # for one and ">= 0.48" for the other. Strategies set this explicitly.
    mode_floor:            float = 0.0
    # The strategy's fee-inclusive EV floor. Executors re-check executable
    # prices against this after reading a fresh order book; zero means a
    # strategy that leaves final price admission to the executor.
    min_net_ev:            float = 0.0
    # Multi-leg decisions (two-sided MAKER quotes, complete-set takes).
    # Empty means "derive a single leg from the flat fields above".
    legs:                  list = field(default_factory=list)

    def is_multi_leg(self) -> bool:
        return bool(self.legs)

    def ensure_legs(self) -> list:
        """The legs to execute, materialising a single leg from flat fields."""
        if self.legs:
            return self.legs
        self.legs = [QuoteLeg(
            outcome=self.outcome,
            outcome_id=self.outcome_id,
            price=float(self.market_price),
            size_pct=float(self.size_pct),
            fair_value=float(self.win_prob),
        )]
        return self.legs

    def total_size_pct(self) -> float:
        """Fraction of bankroll the whole decision commits, all legs included.

        Risk checks must use this, never ``size_pct``: for a two-sided quote
        the exposure-relevant number is both legs together.
        """
        return sum(float(leg.size_pct) for leg in self.ensure_legs())

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
