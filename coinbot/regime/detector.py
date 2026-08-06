"""Market-regime classification.

The bot's answer to "multiple market conditions" starts here: rather than one
strategy that hopes every tape looks the same, the detector scores the current
bar against six named states and the router picks strategies to match.

Scores are deliberately *continuous* and then compared, instead of a chain of
if/else thresholds. That keeps the classifier from flipping on a single tick
across a hard boundary, and hysteresis on top means a new regime must persist
for several bars before strategies are re-routed — regime whiplash is far more
expensive than being a couple of bars late.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..config import RegimeConfig
from ..data.features import FeatureSet
from ..logging_setup import get_logger
from ..models import Regime, RegimeState
from ..util.numeric import clamp, safe_div

log = get_logger("regime")


class RegimeDetector:
    """Classifies one product's market state, with per-product memory."""

    def __init__(self, cfg: Optional[RegimeConfig] = None) -> None:
        self.cfg = cfg or RegimeConfig()
        self._current: Dict[str, Regime] = {}
        self._candidate: Dict[str, Regime] = {}
        self._candidate_count: Dict[str, int] = {}
        self._history: Dict[str, List[Regime]] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def detect(
        self,
        features: FeatureSet,
        htf: Optional[FeatureSet] = None,
    ) -> RegimeState:
        """Classify the newest bar. ``htf`` is the higher-timeframe snapshot."""
        if not features.is_complete:
            return RegimeState(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                detail="indicators still warming up",
            )

        trend = self._trend_score(features, htf)
        vol_pct = features.atr_pct or 0.0
        adx_value = features.adx or 0.0
        scores = self._score_all(features, htf, trend, vol_pct, adx_value)

        candidate = Regime(max(scores, key=lambda k: scores[k]))
        settled = self._apply_hysteresis(features.product_id, candidate, scores)

        # Confidence describes the regime we actually report, not the raw
        # winner. During a hysteresis hold the settled regime may already be
        # losing to its successor, and saying so honestly matters: strategies
        # scale their size by this number.
        settled_score = clamp(scores.get(settled.value, 0.0), 0.0, 1.0)
        rivals = [v for k, v in scores.items() if k != settled.value]
        margin = settled_score - (max(rivals) if rivals else 0.0)
        confidence = clamp(
            0.55 * settled_score + 0.45 * clamp(margin * 2.0, 0.0, 1.0), 0.0, 1.0
        )

        state = RegimeState(
            regime=settled,
            confidence=confidence,
            trend_score=trend,
            volatility_pct=vol_pct,
            adx=adx_value,
            scores={k: round(v, 3) for k, v in scores.items()},
            detail=self._describe(settled, features, trend, vol_pct, adx_value),
        )
        self._history.setdefault(features.product_id, []).append(settled)
        if len(self._history[features.product_id]) > 500:
            del self._history[features.product_id][:100]
        return state

    def current(self, product_id: str) -> Regime:
        return self._current.get(product_id, Regime.UNKNOWN)

    def history(self, product_id: str, n: int = 20) -> List[Regime]:
        return self._history.get(product_id, [])[-n:]

    def reset(self, product_id: Optional[str] = None) -> None:
        if product_id is None:
            self._current.clear()
            self._candidate.clear()
            self._candidate_count.clear()
            self._history.clear()
            return
        self._current.pop(product_id, None)
        self._candidate.pop(product_id, None)
        self._candidate_count.pop(product_id, None)
        self._history.pop(product_id, None)

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #

    def _trend_score(self, f: FeatureSet, htf: Optional[FeatureSet]) -> float:
        """Directional conviction in [-1, +1] from five independent votes."""
        atr = f.atr_or()
        votes: List[float] = []

        if f.ema_fast is not None and f.ema_slow is not None:
            votes.append(clamp((f.ema_fast - f.ema_slow) / atr, -1.0, 1.0))

        if f.ema_trend is not None:
            votes.append(clamp((f.close - f.ema_trend) / (2.5 * atr), -1.0, 1.0))

        if f.plus_di is not None and f.minus_di is not None:
            votes.append(clamp((f.plus_di - f.minus_di) / 25.0, -1.0, 1.0))

        if f.slope is not None:
            # Slope is fraction-of-price per bar; scale against per-bar ATR.
            atr_pct = max(1e-9, safe_div(atr, f.close, 0.01))
            votes.append(clamp(f.slope / atr_pct, -1.0, 1.0))

        if f.macd_hist is not None:
            votes.append(clamp(f.macd_hist / (0.5 * atr), -1.0, 1.0))

        if not votes:
            return 0.0
        score = sum(votes) / len(votes)

        # The higher timeframe gets a vote worth a third of the total, so a
        # 5-minute bounce cannot declare an uptrend against an hourly downtrend.
        if htf is not None and htf.is_complete:
            htf_votes: List[float] = []
            htf_atr = htf.atr_or()
            if htf.ema_fast is not None and htf.ema_slow is not None:
                htf_votes.append(clamp((htf.ema_fast - htf.ema_slow) / (1.5 * htf_atr), -1.0, 1.0))
            if htf.ema_trend is not None:
                htf_votes.append(clamp((htf.close - htf.ema_trend) / (3.0 * htf_atr), -1.0, 1.0))
            if htf_votes:
                score = 0.67 * score + 0.33 * (sum(htf_votes) / len(htf_votes))

        return clamp(score, -1.0, 1.0)

    def _score_all(
        self,
        f: FeatureSet,
        htf: Optional[FeatureSet],
        trend: float,
        vol_pct: float,
        adx_value: float,
    ) -> Dict[str, float]:
        cfg = self.cfg

        # Two smooth ramps over ADX. They overlap deliberately: around the
        # threshold the tape is genuinely ambiguous and both readings should
        # score partially rather than one snapping to zero.
        trend_floor = cfg.adx_trend_threshold - 8.0
        trendiness = clamp(
            (adx_value - trend_floor) / max(1e-9, cfg.adx_strong_threshold - trend_floor),
            0.0, 1.0,
        )
        chop_ceiling = cfg.adx_trend_threshold + 5.0
        choppiness = clamp((chop_ceiling - adx_value) / 18.0, 0.0, 1.0)

        vol_span = max(1e-9, cfg.high_vol_pct - cfg.low_vol_pct)
        vol_level = clamp((vol_pct - cfg.low_vol_pct) / vol_span, 0.0, 1.0)
        is_quiet = 1.0 - vol_level

        width_rank = f.bb_width_rank if f.bb_width_rank is not None else 0.5
        squeeze_room = clamp(
            (cfg.squeeze_percentile - width_rank) / max(1e-9, cfg.squeeze_percentile), 0.0, 1.0
        )

        scores: Dict[str, float] = {}

        # --- Crash: a fast, deep drop. Weighted heavily because the correct
        # response (stand aside) differs from every other regime, and being
        # wrong here is the most expensive mistake the bot can make.
        #
        # The test is deliberately two-sided: the move must be large in
        # absolute terms *and* large relative to this asset's own volatility.
        # A fixed -7% alone would flag routine noise on a coin that swings 5%
        # a bar, while an ATR-only test would flag a quiet asset drifting down.
        drop = self._recent_return(f, cfg.crash_lookback)
        drop_in_atr = self._recent_return_atr(f, cfg.crash_lookback)
        crash = 0.0
        if drop <= cfg.crash_return_pct and drop_in_atr <= -3.0:
            by_pct = clamp(drop / (cfg.crash_return_pct * 2.0), 0.0, 1.0)
            by_atr = clamp(-drop_in_atr / 8.0, 0.0, 1.0)
            crash = 0.70 + 0.30 * max(by_pct, by_atr)
        elif drop <= cfg.crash_return_pct * 0.6 and drop_in_atr <= -4.5:
            crash = 0.55
        scores[Regime.CRASH.value] = crash

        # --- Directional trends. Gated on alignment: without agreement among
        # the trend votes there is no trend, however high ADX reads. ---
        up_alignment = clamp(trend, 0.0, 1.0)
        down_alignment = clamp(-trend, 0.0, 1.0)
        scores[Regime.TRENDING_UP.value] = up_alignment * (0.55 + 0.45 * trendiness)
        scores[Regime.TRENDING_DOWN.value] = down_alignment * (0.55 + 0.45 * trendiness)

        # --- Squeeze: coiling *before* a move. Gated on band compression and
        # on the absence of direction — a steady low-noise trend also has
        # narrow bands, and that is a trend to ride, not a squeeze to wait on.
        squeeze = (
            squeeze_room
            * (0.35 + 0.65 * choppiness)
            * (1.0 - 0.6 * abs(trend))
        )
        if f.in_squeeze:
            squeeze = min(1.0, squeeze + 0.20)
        scores[Regime.SQUEEZE.value] = squeeze

        # --- Ranging: gated on choppiness. Low volatility on its own is not a
        # range — a slow grind higher is quiet and still very much a trend. ---
        centered = 0.0
        if f.percent_b is not None:
            centered = 1.0 - clamp(abs(f.percent_b - 0.5) * 2.0, 0.0, 1.0)
        scores[Regime.RANGING.value] = (
            choppiness * (0.55 + 0.25 * is_quiet + 0.20 * centered) * (1.0 - 0.5 * abs(trend))
        )

        # --- Volatile chop: big ranges, no direction. The worst tape to
        # trade, so it is a named state rather than a fallback. ---
        scores[Regime.VOLATILE_CHOP.value] = (
            vol_level * (0.35 + 0.65 * choppiness) * (1.0 - 0.4 * abs(trend))
        )

        # A confirmed higher-timeframe trend suppresses "there is no trend"
        # readings on the lower timeframe.
        if htf is not None and htf.is_complete and htf.adx is not None:
            if htf.adx > cfg.adx_trend_threshold:
                for key in (Regime.RANGING.value, Regime.VOLATILE_CHOP.value, Regime.SQUEEZE.value):
                    scores[key] *= 0.8

        return scores

    def _recent_return(self, f: FeatureSet, lookback: int) -> float:
        """Return over the last ``lookback`` bars, as a fraction."""
        prior = self._price_ago(f, lookback)
        if prior is None or prior <= 0:
            return 0.0
        return (f.close - prior) / prior

    def _recent_return_atr(self, f: FeatureSet, lookback: int) -> float:
        """The same move expressed in ATR units — volatility-normalized.

        Divided by ``sqrt(lookback)`` so the figure is comparable regardless of
        the window: random walks accumulate range with the square root of time,
        so a raw ATR count would make every long window look extreme.
        """
        prior = self._price_ago(f, lookback)
        if prior is None:
            return 0.0
        expected = f.atr_or() * (max(1, lookback) ** 0.5)
        return safe_div(f.close - prior, expected, 0.0)

    @staticmethod
    def _price_ago(f: FeatureSet, lookback: int) -> Optional[float]:
        candles = f.candles
        if len(candles) <= lookback:
            return None
        return candles[-1 - lookback].close

    # ------------------------------------------------------------------ #
    # Hysteresis
    # ------------------------------------------------------------------ #

    def _apply_hysteresis(
        self,
        product_id: str,
        candidate: Regime,
        scores: Dict[str, float],
    ) -> Regime:
        """Require a new regime to repeat before it takes effect.

        A crash reading is exempt: when the tape falls out of bed the bot
        should be flat now, not two bars from now.
        """
        current = self._current.get(product_id)

        if candidate is Regime.CRASH:
            self._current[product_id] = Regime.CRASH
            self._candidate[product_id] = Regime.CRASH
            self._candidate_count[product_id] = self.cfg.hysteresis_bars
            return Regime.CRASH

        if current is None:
            self._current[product_id] = candidate
            self._candidate[product_id] = candidate
            self._candidate_count[product_id] = self.cfg.hysteresis_bars
            return candidate

        if candidate is current:
            self._candidate[product_id] = candidate
            self._candidate_count[product_id] = self.cfg.hysteresis_bars
            return current

        if self._candidate.get(product_id) is candidate:
            self._candidate_count[product_id] = self._candidate_count.get(product_id, 0) + 1
        else:
            self._candidate[product_id] = candidate
            self._candidate_count[product_id] = 1

        if self._candidate_count[product_id] >= max(1, self.cfg.hysteresis_bars):
            log.info(
                "%s regime %s -> %s (score %.2f)",
                product_id, current.value, candidate.value, scores.get(candidate.value, 0.0),
            )
            self._current[product_id] = candidate
            return candidate

        return current

    # ------------------------------------------------------------------ #
    # Description
    # ------------------------------------------------------------------ #

    def _describe(
        self,
        regime: Regime,
        f: FeatureSet,
        trend: float,
        vol_pct: float,
        adx_value: float,
    ) -> str:
        parts = [
            f"adx={adx_value:.1f}",
            f"atr={vol_pct:.2%}",
            f"trend={trend:+.2f}",
        ]
        if f.bb_width_rank is not None:
            parts.append(f"bbw_pctile={f.bb_width_rank:.0%}")
        if f.rsi is not None:
            parts.append(f"rsi={f.rsi:.0f}")
        return " ".join(parts)


__all__ = ["RegimeDetector"]
