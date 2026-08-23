"""Risk management: how big, whether at all, and when to get out.

This module is deliberately the most conservative code in the project. A
strategy being wrong costs one trade; the risk layer being wrong costs the
account. Every limit is checked before an order is sized, sizing is the
*minimum* of several independent caps, and the circuit breakers can halt
trading entirely without any strategy's cooperation.

The sizing chain, in order:

1. **Risk-based size** — risk a fixed fraction of equity per trade, measured
   to the stop. This is the primary sizer: it makes a wide-stop trade smaller
   and a tight-stop trade larger, so every position risks the same amount.
2. **Volatility target** — cap size so the position's annualized volatility
   contribution stays within budget. Prevents a quiet-looking asset that just
   woke up from dominating the book.
3. **Concentration cap** — no single position above a share of equity.
4. **Exposure cap** — total gross exposure across all positions.
5. **Cash / leverage cap** — spot cannot borrow, so never exceed available cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..config import RiskConfig
from ..data.features import FeatureSet
from ..logging_setup import get_logger
from ..models import (
    AccountSnapshot,
    Candle,
    Position,
    Product,
    Regime,
    SignalAction,
    TradeRecord,
)
from ..portfolio.book import PositionBook
from ..util.numeric import clamp, round_size, safe_div, to_decimal
from ..util.timing import now_ts

log = get_logger("risk")

_DAY = 86400.0
_WEEK = 7 * _DAY


@dataclass
class RiskVerdict:
    """Whether a proposed entry is permitted."""

    allowed: bool
    reason: str = ""
    scale: float = 1.0  # optional size haircut when allowed but cautious

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def ok(cls, scale: float = 1.0, reason: str = "") -> "RiskVerdict":
        return cls(True, reason, scale)

    @classmethod
    def block(cls, reason: str) -> "RiskVerdict":
        return cls(False, reason, 0.0)


@dataclass
class SizingResult:
    """The outcome of sizing one entry."""

    size: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    risk_amount: Decimal = Decimal(0)
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    binding_constraint: str = ""
    detail: Dict[str, float] = field(default_factory=dict)
    rejected: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.size > 0 and not self.rejected


@dataclass
class ExitCheck:
    """Why an open position should be closed, if it should."""

    should_exit: bool
    reason: str = ""
    urgent: bool = False  # market-order out rather than working a limit

    @classmethod
    def stay(cls) -> "ExitCheck":
        return cls(False)


class RiskManager:
    """Owns every limit between a signal and an order."""

    def __init__(self, cfg: Optional[RiskConfig] = None) -> None:
        self.cfg = cfg or RiskConfig()
        self._halted = False
        self._halt_reason = ""
        self._halted_until: float = 0.0
        self._cooldowns: Dict[str, int] = {}       # product -> bars remaining
        self._day_start_equity: Optional[Decimal] = None
        self._week_start_equity: Optional[Decimal] = None
        self._day_anchor: float = 0.0
        self._week_anchor: float = 0.0
        self._returns_cache: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------ #
    # Circuit breakers
    # ------------------------------------------------------------------ #

    @property
    def halted(self) -> bool:
        if self._halted and self._halted_until and now_ts() >= self._halted_until:
            log.warning("trading cooldown expired; resuming (was: %s)", self._halt_reason)
            self._halted = False
            self._halt_reason = ""
            self._halted_until = 0.0
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def halt(self, reason: str, *, seconds: Optional[float] = None) -> None:
        if not self._halted:
            log.critical("TRADING HALTED: %s", reason)
        self._halted = True
        self._halt_reason = reason
        self._halted_until = (now_ts() + seconds) if seconds else 0.0

    def resume(self) -> None:
        """Clear a halt. Manual action — breakers never self-clear on loss."""
        if self._halted:
            log.warning("trading manually resumed (was halted: %s)", self._halt_reason)
        self._halted = False
        self._halt_reason = ""
        self._halted_until = 0.0

    def check_circuit_breakers(
        self,
        book: PositionBook,
        equity: Decimal,
        *,
        ts: Optional[float] = None,
    ) -> RiskVerdict:
        """Evaluate every account-level breaker. Call once per loop."""
        cfg = self.cfg
        ts = ts if ts is not None else now_ts()
        self._roll_periods(equity, ts)
        book.stats.mark_equity(equity, ts)

        if self.halted:
            return RiskVerdict.block(f"halted: {self._halt_reason}")

        # --- Maximum drawdown from the equity peak ---
        drawdown = book.stats.drawdown_from_peak(equity)
        if drawdown >= cfg.max_drawdown_pct:
            reason = (
                f"max drawdown breached: {drawdown:.2%} >= {cfg.max_drawdown_pct:.2%} "
                f"(peak {book.stats.peak_equity:.2f}, now {equity:.2f})"
            )
            if cfg.halt_on_drawdown:
                # No auto-expiry: a drawdown halt needs a human to look at it.
                self.halt(reason)
            return RiskVerdict.block(reason)

        # --- Daily loss limit ---
        if self._day_start_equity and self._day_start_equity > 0:
            day_pnl = float((equity - self._day_start_equity) / self._day_start_equity)
            if day_pnl <= -cfg.daily_loss_limit_pct:
                reason = f"daily loss limit hit: {day_pnl:.2%}"
                self.halt(reason, seconds=self._seconds_until_next_day(ts))
                return RiskVerdict.block(reason)

        # --- Weekly loss limit ---
        if self._week_start_equity and self._week_start_equity > 0:
            week_pnl = float((equity - self._week_start_equity) / self._week_start_equity)
            if week_pnl <= -cfg.weekly_loss_limit_pct:
                reason = f"weekly loss limit hit: {week_pnl:.2%}"
                self.halt(reason, seconds=_WEEK - (ts - self._week_anchor))
                return RiskVerdict.block(reason)

        # --- Losing streak ---
        if book.stats.consecutive_losses >= cfg.max_consecutive_losses:
            reason = f"{book.stats.consecutive_losses} consecutive losses"
            self.halt(reason, seconds=cfg.cooldown_bars_after_breaker * 60.0)
            return RiskVerdict.block(reason)

        return RiskVerdict.ok()

    def _roll_periods(self, equity: Decimal, ts: float) -> None:
        """Reset the daily/weekly PnL anchors when a period rolls over."""
        if self._day_start_equity is None:
            self._day_start_equity = equity
            self._day_anchor = ts
        elif ts - self._day_anchor >= _DAY:
            self._day_start_equity = equity
            self._day_anchor = ts

        if self._week_start_equity is None:
            self._week_start_equity = equity
            self._week_anchor = ts
        elif ts - self._week_anchor >= _WEEK:
            self._week_start_equity = equity
            self._week_anchor = ts

    @staticmethod
    def _seconds_until_next_day(ts: float) -> float:
        return _DAY - (ts % _DAY)

    # ------------------------------------------------------------------ #
    # Cooldowns
    # ------------------------------------------------------------------ #

    def start_cooldown(self, product_id: str, bars: Optional[int] = None) -> None:
        count = self.cfg.cooldown_bars_after_loss if bars is None else bars
        if count > 0:
            self._cooldowns[product_id] = count

    def tick_cooldowns(self) -> None:
        for product_id in list(self._cooldowns):
            self._cooldowns[product_id] -= 1
            if self._cooldowns[product_id] <= 0:
                del self._cooldowns[product_id]

    def in_cooldown(self, product_id: str) -> bool:
        return self._cooldowns.get(product_id, 0) > 0

    def on_trade_closed(self, trade: TradeRecord) -> None:
        """Start a cooldown after a loss so the bot does not revenge-trade."""
        if not trade.is_win:
            self.start_cooldown(trade.product_id)

    # ------------------------------------------------------------------ #
    # Entry permission
    # ------------------------------------------------------------------ #

    def check_entry(
        self,
        product_id: str,
        direction: int,
        book: PositionBook,
        equity: Decimal,
        *,
        features: Optional[FeatureSet] = None,
        regime: Regime = Regime.UNKNOWN,
        spread_bps: Optional[float] = None,
        candles_by_product: Optional[Dict[str, Sequence[Candle]]] = None,
    ) -> RiskVerdict:
        """Every account- and portfolio-level gate on opening a position."""
        cfg = self.cfg

        if self.halted:
            return RiskVerdict.block(f"halted: {self._halt_reason}")

        if regime is Regime.CRASH:
            return RiskVerdict.block("crash regime: no new entries")

        if self.in_cooldown(product_id):
            return RiskVerdict.block(
                f"cooldown active ({self._cooldowns[product_id]} bars left)"
            )

        if book.has_position(product_id):
            return RiskVerdict.block("already holding this product")

        if book.open_count >= cfg.max_open_positions:
            return RiskVerdict.block(
                f"at max open positions ({book.open_count}/{cfg.max_open_positions})"
            )

        same_direction = book.long_count() if direction > 0 else book.short_count()
        if same_direction >= cfg.max_same_direction_positions:
            return RiskVerdict.block(
                f"too many positions in the same direction ({same_direction})"
            )

        if equity <= 0:
            return RiskVerdict.block("no equity")

        exposure_pct = float(safe_div(float(book.gross_exposure), float(equity), 0.0))
        if exposure_pct >= cfg.max_gross_exposure_pct:
            return RiskVerdict.block(
                f"gross exposure {exposure_pct:.1%} at cap {cfg.max_gross_exposure_pct:.1%}"
            )

        if spread_bps is not None and spread_bps > cfg.max_spread_bps:
            return RiskVerdict.block(
                f"spread {spread_bps:.1f}bps above {cfg.max_spread_bps:.1f}bps limit"
            )

        # Highly correlated positions are one bet wearing two hats.
        if candles_by_product:
            correlated = self._correlated_holding(product_id, book, candles_by_product)
            if correlated:
                other, rho = correlated
                return RiskVerdict.block(
                    f"correlation {rho:.2f} with open {other} exceeds "
                    f"{cfg.correlation_cap:.2f}"
                )

        # Volatility haircut: still allowed, but smaller when the tape is wild.
        scale = 1.0
        if features is not None and features.atr_pct:
            if features.atr_pct > 0.06:
                scale = 0.6
            elif features.atr_pct > 0.04:
                scale = 0.8

        return RiskVerdict.ok(scale)

    def _correlated_holding(
        self,
        product_id: str,
        book: PositionBook,
        candles_by_product: Dict[str, Sequence[Candle]],
    ):
        """Find an open position too correlated with the proposed one."""
        target = self._returns(product_id, candles_by_product)
        if not target:
            return None
        for position in book.open_positions():
            other = self._returns(position.product_id, candles_by_product)
            if not other:
                continue
            rho = _correlation(target, other)
            if rho >= self.cfg.correlation_cap:
                return position.product_id, rho
        return None

    def _returns(
        self,
        product_id: str,
        candles_by_product: Dict[str, Sequence[Candle]],
        lookback: int = 96,
    ) -> List[float]:
        candles = candles_by_product.get(product_id)
        if not candles or len(candles) < 20:
            return []
        window = list(candles)[-(lookback + 1):]
        out: List[float] = []
        for prev, cur in zip(window, window[1:]):
            if prev.close > 0:
                out.append((cur.close - prev.close) / prev.close)
        return out

    # ------------------------------------------------------------------ #
    # Sizing
    # ------------------------------------------------------------------ #

    def size_position(
        self,
        *,
        product: Product,
        price: Decimal,
        direction: int,
        stop_price: Optional[Decimal],
        conviction: float,
        equity: Decimal,
        available_cash: Decimal,
        book: PositionBook,
        features: Optional[FeatureSet] = None,
        take_profit: Optional[Decimal] = None,
        scale: float = 1.0,
        bars_per_year: int = 105_120,
    ) -> SizingResult:
        """Compute the position size, as the minimum of every applicable cap."""
        cfg = self.cfg
        price = to_decimal(price)
        if price <= 0:
            return SizingResult(rejected="invalid price")
        if direction == 0:
            return SizingResult(rejected="no direction")

        conviction = clamp(conviction, 0.0, 1.0) * clamp(scale, 0.0, 1.0)
        if conviction <= 0:
            return SizingResult(rejected="zero conviction")

        # --- 1. Risk-based size ---------------------------------------- #
        stop = to_decimal(stop_price) if stop_price is not None else None
        if (
            cfg.override_strategy_exits
            or stop is None
            or stop <= 0
            or (direction > 0 and stop >= price)
            or (direction < 0 and stop <= price)
        ):
            # Either the operator has taken control of exits portfolio-wide, or
            # there is no usable stop and we must not size against a zero (or
            # negative) risk distance.
            atr = to_decimal(features.atr_or() if features else float(price) * 0.01)
            stop = price - to_decimal(direction) * to_decimal(cfg.atr_stop_multiple) * atr

        stop_distance = abs(price - stop)
        if stop_distance <= 0:
            return SizingResult(rejected="stop distance is zero")

        # A stop wider than the hard cap would size the position absurdly small
        # and risk more than intended if it gapped — clamp it instead.
        max_distance = price * to_decimal(cfg.hard_stop_pct)
        if stop_distance > max_distance:
            stop_distance = max_distance
            stop = price - to_decimal(direction) * stop_distance

        risk_budget = equity * to_decimal(cfg.risk_per_trade_pct) * to_decimal(conviction)
        risk_size = risk_budget / stop_distance
        candidates: Dict[str, Decimal] = {"risk_per_trade": risk_size}

        # --- 2. Volatility target -------------------------------------- #
        if cfg.use_vol_targeting and features is not None:
            asset_vol = self._annualized_vol(features, bars_per_year)
            if asset_vol > 0:
                target_notional = equity * to_decimal(cfg.target_annual_vol / asset_vol)
                candidates["vol_target"] = target_notional / price

        # --- 3. Concentration cap -------------------------------------- #
        candidates["max_position_pct"] = (equity * to_decimal(cfg.max_position_pct)) / price

        # --- 4. Remaining gross exposure ------------------------------- #
        exposure_room = equity * to_decimal(cfg.max_gross_exposure_pct) - book.gross_exposure
        candidates["gross_exposure"] = max(Decimal(0), exposure_room) / price

        # --- 5. Cash / leverage ---------------------------------------- #
        # Spot cannot borrow. Leave a small buffer so fees never overdraw.
        spendable = min(available_cash, equity * to_decimal(cfg.max_leverage))
        candidates["available_cash"] = max(Decimal(0), spendable * Decimal("0.995")) / price

        binding = min(candidates, key=lambda k: candidates[k])
        raw_size = max(Decimal(0), candidates[binding])

        size = round_size(raw_size, product.base_increment)
        notional = size * price

        result = SizingResult(
            size=size,
            notional=notional,
            risk_amount=size * stop_distance,
            stop_price=stop,
            take_profit=self._target_for(
                price, direction, stop_distance, proposed=take_profit
            ),
            binding_constraint=binding,
            detail={k: float(v * price) for k, v in candidates.items()},
        )

        # --- Minimums -------------------------------------------------- #
        if size <= 0:
            result.rejected = f"size rounds to zero (limited by {binding})"
        elif notional < to_decimal(cfg.min_position_notional):
            result.rejected = (
                f"notional {notional:.2f} below minimum "
                f"{cfg.min_position_notional:.2f} (limited by {binding})"
            )
        elif product.min_base_size and size < product.min_base_size:
            result.rejected = (
                f"size {size} below exchange minimum {product.min_base_size}"
            )
        elif product.min_quote_size and notional < product.min_quote_size:
            result.rejected = (
                f"notional {notional:.2f} below exchange minimum {product.min_quote_size}"
            )
        return result

    def _target_for(
        self,
        price: Decimal,
        direction: int,
        stop_distance: Decimal,
        *,
        proposed: Optional[Decimal],
    ) -> Optional[Decimal]:
        """The take-profit to attach — the strategy's, or one from ``take_profit_r``.

        ``take_profit_r`` is expressed in multiples of the trade's *initial
        risk*, so it is measured off the stop distance actually being used
        after every clamp above. A target set from the pre-clamp distance would
        promise a reward-to-risk the position does not have.

        Set ``take_profit_r`` to 0 to run without a fixed target and let the
        trailing stop decide when to leave.
        """
        cfg = self.cfg
        strategy_target = to_decimal(proposed) if proposed is not None else None
        if strategy_target is not None and not cfg.override_strategy_exits:
            return strategy_target
        if cfg.take_profit_r <= 0:
            return None if cfg.override_strategy_exits else strategy_target
        if stop_distance <= 0:
            return strategy_target
        return price + to_decimal(direction) * to_decimal(cfg.take_profit_r) * stop_distance

    def _annualized_vol(self, features: FeatureSet, bars_per_year: int) -> float:
        """Annualized volatility estimate, preferring realized over ATR."""
        if features.realized_vol and features.realized_vol > 0:
            return features.realized_vol
        if features.atr_pct and features.atr_pct > 0:
            # ATR is a per-bar range; scale to a yearly standard deviation.
            return features.atr_pct * math.sqrt(bars_per_year)
        return 0.0

    # ------------------------------------------------------------------ #
    # Stop management
    # ------------------------------------------------------------------ #

    def initial_stop(
        self,
        price: Decimal,
        direction: int,
        features: Optional[FeatureSet],
        proposed: Optional[Decimal] = None,
    ) -> Decimal:
        """The stop to attach at entry — the strategy's, or an ATR default."""
        atr = to_decimal(features.atr_or() if features else float(price) * 0.01)
        default = price - to_decimal(direction) * to_decimal(self.cfg.atr_stop_multiple) * atr
        if proposed is None or self.cfg.override_strategy_exits:
            return default
        proposed = to_decimal(proposed)
        # Reject a stop on the wrong side of the market.
        if direction > 0 and proposed >= price:
            return default
        if direction < 0 and proposed <= price:
            return default
        # Never allow a stop wider than the hard cap.
        if abs(price - proposed) > price * to_decimal(self.cfg.hard_stop_pct):
            return price - to_decimal(direction) * price * to_decimal(self.cfg.hard_stop_pct)
        return proposed

    def update_stops(self, position: Position, features: FeatureSet) -> bool:
        """Advance breakeven and trailing stops. Returns True if the stop moved.

        Stops only ever move in the favourable direction. A stop that can loosen
        is not a stop.
        """
        cfg = self.cfg
        if not position.is_open or not position.entry_price:
            return False

        price = to_decimal(features.close)
        atr = to_decimal(features.atr_or())
        direction = 1 if position.is_long else -1
        moved = False

        # Track the best price seen, which the trail hangs from.
        if position.trail_anchor is None:
            position.trail_anchor = price
        elif direction > 0 and price > position.trail_anchor:
            position.trail_anchor = price
        elif direction < 0 and price < position.trail_anchor:
            position.trail_anchor = price

        initial_risk = abs(position.entry_price - position.stop_price) if position.stop_price else atr
        if initial_risk <= 0:
            initial_risk = atr

        gain = (price - position.entry_price) * to_decimal(direction)
        r_multiple = float(gain / initial_risk) if initial_risk else 0.0

        # --- Breakeven: once the trade has paid for itself, stop risking. ---
        if cfg.breakeven_at_r > 0 and r_multiple >= cfg.breakeven_at_r:
            breakeven = position.entry_price
            if self._is_tighter(position.stop_price, breakeven, direction):
                position.stop_price = breakeven
                moved = True

        # --- Trailing stop ---
        if cfg.use_trailing_stop and position.trail_anchor:
            trail = position.trail_anchor - to_decimal(direction) * to_decimal(
                cfg.atr_trail_multiple
            ) * atr
            if self._is_tighter(position.stop_price, trail, direction):
                position.stop_price = trail
                moved = True

        return moved

    @staticmethod
    def _is_tighter(current: Optional[Decimal], candidate: Decimal, direction: int) -> bool:
        """True if ``candidate`` is a strictly better (tighter) stop."""
        if current is None:
            return True
        return candidate > current if direction > 0 else candidate < current

    # ------------------------------------------------------------------ #
    # Exit checks
    # ------------------------------------------------------------------ #

    def check_exit(
        self,
        position: Position,
        features: FeatureSet,
        *,
        bar: Optional[Candle] = None,
    ) -> ExitCheck:
        """Stop, target, time and disaster checks for an open position.

        The intrabar high/low is used when available, because a stop is hit the
        moment price *touches* it, not when the bar happens to close beyond it.
        """
        cfg = self.cfg
        if not position.is_open:
            return ExitCheck.stay()

        direction = 1 if position.is_long else -1
        close = to_decimal(features.close)
        low = to_decimal(bar.low) if bar else close
        high = to_decimal(bar.high) if bar else close

        # --- Protective stop ---
        if position.stop_price is not None:
            if direction > 0 and low <= position.stop_price:
                return ExitCheck(True, f"stop hit at {position.stop_price}", urgent=True)
            if direction < 0 and high >= position.stop_price:
                return ExitCheck(True, f"stop hit at {position.stop_price}", urgent=True)

        # --- Take profit ---
        if position.take_profit is not None:
            if direction > 0 and high >= position.take_profit:
                return ExitCheck(True, f"target hit at {position.take_profit}")
            if direction < 0 and low <= position.take_profit:
                return ExitCheck(True, f"target hit at {position.take_profit}")

        # --- Hard percentage backstop, in case the stop was never set ---
        if position.entry_price > 0:
            move = float((close - position.entry_price) / position.entry_price) * direction
            if move <= -cfg.hard_stop_pct:
                return ExitCheck(
                    True, f"hard stop: {move:.2%} against entry", urgent=True
                )

        # --- Time stop: capital tied up in a trade going nowhere ---
        if cfg.max_bars_in_trade > 0 and position.bars_held >= cfg.max_bars_in_trade:
            return ExitCheck(True, f"time stop after {position.bars_held} bars")

        return ExitCheck.stay()

    def emergency_exit_all(self, reason: str) -> None:
        self.halt(f"emergency: {reason}")

    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, object]:
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "cooldowns": dict(self._cooldowns),
            "day_start_equity": float(self._day_start_equity or 0),
            "week_start_equity": float(self._week_start_equity or 0),
        }


def _correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation over the overlapping tail of two return series."""
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    x, y = list(a[-n:]), list(b[-n:])
    mean_x = math.fsum(x) / n
    mean_y = math.fsum(y) / n
    cov = math.fsum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = math.fsum((xi - mean_x) ** 2 for xi in x)
    var_y = math.fsum((yi - mean_y) ** 2 for yi in y)
    denom = math.sqrt(var_x * var_y)
    if denom <= 0:
        return 0.0
    return clamp(cov / denom, -1.0, 1.0)


__all__ = ["ExitCheck", "RiskManager", "RiskVerdict", "SizingResult"]
