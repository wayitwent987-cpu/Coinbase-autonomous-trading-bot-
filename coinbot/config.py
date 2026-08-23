"""Typed configuration: YAML file -> dataclasses, with env-var overrides.

Precedence (lowest to highest): dataclass defaults < YAML file < environment.
Secrets are *only* ever read from the environment or a key file — never from
the committed config — so a checked-in YAML can never leak an API key.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # PyYAML is in requirements, but keep the import survivable.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from .models import Granularity

log = logging.getLogger(__name__)

ENV_PREFIX = "COINBOT_"


class ConfigError(Exception):
    """Raised when configuration is missing or self-contradictory."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


@dataclass
class ApiConfig:
    """Coinbase Advanced Trade credentials and endpoints.

    ``api_key`` is the CDP key name
    (``organizations/{org_id}/apiKeys/{key_id}``) and ``api_secret`` is the
    EC private key PEM. Both come from the environment
    (``COINBOT_API_KEY`` / ``COINBOT_API_SECRET``) or a downloaded CDP JSON
    key file pointed at by ``key_file``.
    """

    api_key: str = ""
    api_secret: str = ""
    key_file: str = ""
    base_url: str = "https://api.coinbase.com"
    ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    api_prefix: str = "/api/v3/brokerage"
    timeout_s: float = 20.0
    max_retries: int = 4
    rate_limit_per_sec: float = 8.0
    jwt_ttl_s: int = 120

    def load_key_file(self) -> None:
        """Populate credentials from a CDP-downloaded JSON key file."""
        if not self.key_file:
            return
        path = Path(self.key_file).expanduser()
        if not path.exists():
            raise ConfigError(f"api.key_file not found: {path}")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"api.key_file is not valid JSON: {path}") from exc
        self.api_key = self.api_key or data.get("name", "") or data.get("id", "")
        self.api_secret = self.api_secret or data.get("privateKey", "") or data.get(
            "private_key", ""
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class TradingConfig:
    """What to trade and on what clock."""

    products: List[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD"])
    quote_currency: str = "USD"
    granularity: str = Granularity.FIVE_MINUTE.value
    # Higher timeframe used only to bias direction / classify the regime.
    htf_granularity: str = Granularity.ONE_HOUR.value
    warmup_bars: int = 300
    # 400 bars is 2x the slowest indicator's period (EMA200). Retaining more
    # roughly doubles backtest time for no change in decisions. If you add an
    # indicator slower than EMA200, raise this and warmup_bars together.
    max_candles: int = 400
    loop_interval_s: int = 0  # 0 = align to bar close
    bar_close_offset_s: float = 3.0
    allow_shorts: bool = False
    dry_run: bool = True

    @property
    def granularity_enum(self) -> Granularity:
        return Granularity(self.granularity)

    @property
    def htf_granularity_enum(self) -> Granularity:
        return Granularity(self.htf_granularity)

    @property
    def bar_seconds(self) -> int:
        return self.granularity_enum.seconds


@dataclass
class RiskConfig:
    """Position sizing and the hard limits that stop the bot."""

    # Sizing
    risk_per_trade_pct: float = 0.0075       # 0.75% of equity risked per trade
    max_position_pct: float = 0.25           # cap one position at 25% of equity
    min_position_notional: float = 10.0      # skip dust trades
    target_annual_vol: float = 0.60          # vol-targeting budget
    use_vol_targeting: bool = True
    max_leverage: float = 1.0                # spot: never exceed equity

    # Exposure
    max_open_positions: int = 4
    max_gross_exposure_pct: float = 0.90
    max_same_direction_positions: int = 3
    correlation_cap: float = 0.85            # treat >cap products as one bet

    # Stops
    atr_stop_multiple: float = 2.2
    atr_trail_multiple: float = 3.0
    take_profit_r: float = 2.5               # in multiples of initial risk
    use_trailing_stop: bool = True
    breakeven_at_r: float = 1.0              # move stop to entry after +1R
    max_bars_in_trade: int = 288             # time stop
    hard_stop_pct: float = 0.12              # absolute backstop per position

    # Circuit breakers
    daily_loss_limit_pct: float = 0.03
    weekly_loss_limit_pct: float = 0.07
    max_drawdown_pct: float = 0.15
    max_consecutive_losses: int = 6
    cooldown_bars_after_loss: int = 2
    cooldown_bars_after_breaker: int = 60
    halt_on_drawdown: bool = True

    # Execution guards
    max_spread_bps: float = 25.0
    max_slippage_bps: float = 30.0
    min_24h_volume_quote: float = 1_000_000.0


@dataclass
class ExecutionConfig:
    """How orders reach the exchange."""

    order_type: str = "limit"          # limit | market
    post_only: bool = False
    limit_offset_bps: float = 2.0      # how far inside the spread to price
    chase_limit_bars: int = 2          # re-price this many times before market
    max_order_retries: int = 3
    taker_fee_bps: float = 60.0        # conservative default tier
    maker_fee_bps: float = 40.0
    slippage_bps: float = 5.0          # assumed for paper/backtest fills
    cancel_stale_after_s: float = 90.0
    use_native_stop_orders: bool = False  # else stops are managed locally


@dataclass
class RegimeConfig:
    """Thresholds for the market-state classifier."""

    adx_period: int = 14
    adx_trend_threshold: float = 23.0
    adx_strong_threshold: float = 35.0
    atr_period: int = 14
    vol_lookback: int = 96
    high_vol_pct: float = 0.045        # ATR/price above this = volatile
    low_vol_pct: float = 0.012         # below this = quiet/squeeze
    trend_fast_ema: int = 21
    trend_slow_ema: int = 55
    trend_confirm_ema: int = 200
    bb_period: int = 20
    bb_std: float = 2.0
    squeeze_percentile: float = 0.20   # BB width in bottom 20% = squeeze
    crash_return_pct: float = -0.07    # drop over crash_lookback triggers crash
    crash_lookback: int = 12
    hysteresis_bars: int = 2           # bars a new regime must persist


@dataclass
class StrategyConfig:
    """Per-strategy switches and the regime -> strategy routing table."""

    enabled: List[str] = field(
        default_factory=lambda: [
            "trend_follow",
            "breakout",
            "mean_reversion",
            "range_scalp",
            "squeeze_break",
        ]
    )
    min_signal_strength: float = 0.35
    require_htf_alignment: bool = True
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    routing: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {
            "TRENDING_UP": {"trend_follow": 1.0, "breakout": 0.8, "squeeze_break": 0.6},
            "TRENDING_DOWN": {"trend_follow": 1.0, "breakout": 0.6, "squeeze_break": 0.4},
            "RANGING": {"mean_reversion": 1.0, "range_scalp": 0.7},
            "VOLATILE_CHOP": {"mean_reversion": 0.4},
            "SQUEEZE": {"squeeze_break": 1.0, "breakout": 0.5},
            "CRASH": {},
            "UNKNOWN": {},
        }
    )

    def weights_for(self, regime: str) -> Dict[str, float]:
        table = self.routing.get(regime, {})
        return {k: v for k, v in table.items() if k in self.enabled and v > 0}


@dataclass
class AdaptiveConfig:
    """Scale position size by how a strategy has actually performed.

    This shrinks trades in (strategy, regime) combinations that have lost money
    and modestly grows those that have earned it. It reduces repetition of a
    demonstrated losing pattern; it does not predict the market.
    """

    enabled: bool = True
    # Shrinkage constant: at this many trades the observed record carries half
    # its full weight. Lower = adapts faster and chases noise more.
    confidence_trades: int = 20
    # Hard evidence floor: below this many trades the tracker stays neutral
    # rather than reacting to a handful of results.
    min_trades: int = 5
    # Average net return per trade treated as "good" when scoring.
    reference_return: float = 0.005
    # Never size to zero: a banned combination stops producing the evidence
    # needed to notice that conditions changed.
    floor: float = 0.25
    # Kept close to 1.0 on purpose. Sizing up after a winning streak is how
    # accounts blow up, because streaks are the most common artifact of noise.
    ceiling: float = 1.25
    gain: float = 1.0
    window: int = 50


@dataclass
class StorageConfig:
    db_path: str = "state/coinbot.sqlite3"
    persist_candles: bool = False
    journal_trades: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/coinbot.log"
    json_format: bool = False
    max_bytes: int = 10_485_760
    backup_count: int = 5
    console: bool = True


@dataclass
class BacktestConfig:
    start: str = ""            # ISO date, empty = as far back as data allows
    end: str = ""
    initial_equity: float = 10_000.0
    fee_bps: float = 60.0
    slippage_bps: float = 5.0
    data_dir: str = "data"


@dataclass
class Config:
    """Root configuration object."""

    api: ApiConfig = field(default_factory=ApiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        for f in fields(cls):
            section = data.get(f.name)
            if section is None:
                continue
            if not isinstance(section, dict):
                raise ConfigError(f"config section '{f.name}' must be a mapping")
            _apply_section(getattr(cfg, f.name), section, f.name)
        return cfg

    @classmethod
    def load(cls, path: Optional[str] = None, *, apply_env: bool = True) -> "Config":
        data: Dict[str, Any] = {}
        if path:
            file_path = Path(path).expanduser()
            if not file_path.exists():
                raise ConfigError(f"config file not found: {file_path}")
            if yaml is None:
                raise ConfigError("PyYAML is required to read YAML config files")
            loaded = yaml.safe_load(file_path.read_text()) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"config root must be a mapping: {file_path}")
            data = loaded
        cfg = cls.from_dict(data)
        if apply_env:
            cfg.apply_env()
        cfg.api.load_key_file()
        cfg.validate()
        return cfg

    def apply_env(self, environ: Optional[Dict[str, str]] = None) -> None:
        """Overlay ``COINBOT_SECTION_FIELD`` environment variables.

        ``COINBOT_RISK_RISK_PER_TRADE_PCT=0.01`` sets ``risk.risk_per_trade_pct``.
        The common credential vars have short aliases.
        """
        env = dict(environ if environ is not None else os.environ)

        aliases = {
            "COINBASE_API_KEY": ("api", "api_key"),
            "COINBASE_API_SECRET": ("api", "api_secret"),
            "COINBOT_API_KEY": ("api", "api_key"),
            "COINBOT_API_SECRET": ("api", "api_secret"),
            "COINBOT_KEY_FILE": ("api", "key_file"),
            "COINBOT_DRY_RUN": ("trading", "dry_run"),
            "COINBOT_PRODUCTS": ("trading", "products"),
            "COINBOT_LOG_LEVEL": ("logging", "level"),
        }
        for env_name, (section, attr) in aliases.items():
            if env_name in env and env[env_name] != "":
                target = getattr(self, section)
                _set_field(target, attr, env[env_name], f"{section}.{attr}")

        section_names = {f.name for f in fields(self)}
        for key, raw in env.items():
            if not key.startswith(ENV_PREFIX) or key in aliases:
                continue
            remainder = key[len(ENV_PREFIX):].lower()
            # Unknown options are an error, not a shrug. A typo like
            # COINBOT_RISK_MAX_DRAWDOWN (the real name ends in _pct) would
            # otherwise be silently discarded and the bot would run with a
            # risk limit the operator believes they changed.
            matched = False
            for name in sorted(section_names, key=len, reverse=True):
                prefix = f"{name}_"
                if remainder.startswith(prefix):
                    attr = remainder[len(prefix):]
                    target = getattr(self, name)
                    if any(f.name == attr for f in fields(target)):
                        _set_field(target, attr, raw, f"{name}.{attr}")
                        matched = True
                    break
            if not matched:
                raise ConfigError(
                    f"unknown config option from environment variable '{key}'"
                )

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate(self) -> None:
        problems: List[str] = []
        t, r, e = self.trading, self.risk, self.execution

        if not t.products:
            problems.append("trading.products is empty")
        for product in t.products:
            if "-" not in product:
                problems.append(f"trading.products: '{product}' is not a PRODUCT-QUOTE id")
        granularity_ok = True
        try:
            Granularity(t.granularity)
        except ValueError:
            granularity_ok = False
            problems.append(f"trading.granularity '{t.granularity}' is not supported")
        try:
            Granularity(t.htf_granularity)
        except ValueError:
            granularity_ok = False
            problems.append(f"trading.htf_granularity '{t.htf_granularity}' is not supported")
        # Only compare once *both* parse — otherwise reading .granularity_enum
        # re-raises the ValueError and validate() dies with a raw traceback
        # instead of the collected list of problems.
        if granularity_ok and t.htf_granularity_enum.seconds < t.granularity_enum.seconds:
            problems.append("trading.htf_granularity must be >= trading.granularity")

        if not 0 < r.risk_per_trade_pct <= 0.1:
            problems.append("risk.risk_per_trade_pct must be in (0, 0.10]")
        if not 0 < r.max_position_pct <= 1.0:
            problems.append("risk.max_position_pct must be in (0, 1]")
        if r.max_open_positions < 1:
            problems.append("risk.max_open_positions must be >= 1")
        if not 0 < r.max_drawdown_pct <= 1.0:
            problems.append("risk.max_drawdown_pct must be in (0, 1]")
        if r.atr_stop_multiple <= 0:
            problems.append("risk.atr_stop_multiple must be > 0")
        if r.max_gross_exposure_pct <= 0:
            problems.append("risk.max_gross_exposure_pct must be > 0")
        if r.max_leverage < 1.0 and r.max_gross_exposure_pct > r.max_leverage:
            problems.append("risk.max_gross_exposure_pct exceeds risk.max_leverage")

        if e.order_type not in ("limit", "market"):
            problems.append("execution.order_type must be 'limit' or 'market'")
        if e.post_only and e.order_type != "limit":
            problems.append("execution.post_only requires order_type 'limit'")

        if t.warmup_bars < 60:
            problems.append("trading.warmup_bars should be >= 60 for stable indicators")
        if t.max_candles < t.warmup_bars:
            problems.append("trading.max_candles must be >= trading.warmup_bars")

        unknown = set(self.strategy.enabled) - set(KNOWN_STRATEGIES)
        if unknown:
            problems.append(
                f"strategy.enabled has unknown entries: {sorted(unknown)}; "
                f"known: {sorted(KNOWN_STRATEGIES)}"
            )

        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))

    def require_credentials(self) -> None:
        if not self.api.has_credentials:
            raise ConfigError(
                "Coinbase credentials missing. Set COINBOT_API_KEY and "
                "COINBOT_API_SECRET, or point api.key_file at a CDP key JSON."
            )

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #

    def to_dict(self, *, redact: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if redact:
            api = data.get("api", {})
            for key in ("api_key", "api_secret"):
                if api.get(key):
                    api[key] = "***redacted***"
        return data

    def to_yaml(self, *, redact: bool = True) -> str:
        if yaml is None:  # pragma: no cover
            return json.dumps(self.to_dict(redact=redact), indent=2, default=str)
        return yaml.safe_dump(self.to_dict(redact=redact), sort_keys=False, default_flow_style=False)

    def summary(self) -> str:
        mode = "DRY-RUN (paper)" if self.trading.dry_run else "LIVE TRADING"
        return (
            f"mode={mode} products={','.join(self.trading.products)} "
            f"tf={self.trading.granularity} htf={self.trading.htf_granularity} "
            f"risk/trade={self.risk.risk_per_trade_pct:.2%} "
            f"max_dd={self.risk.max_drawdown_pct:.1%} "
            f"strategies={','.join(self.strategy.enabled)}"
        )


KNOWN_STRATEGIES = (
    "trend_follow",
    "breakout",
    "mean_reversion",
    "range_scalp",
    "squeeze_break",
)


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def _type_name(target_type: Any) -> str:
    """Normalize a field annotation to a lowercase base-type name.

    ``from __future__ import annotations`` makes every ``dataclasses.Field.type``
    a *string* (``'float'``, ``'List[str]'``, ``'Optional[Decimal]'``), so this
    has to handle both real types and their textual form.
    """
    if isinstance(target_type, str):
        text = target_type.strip()
        # Unwrap Optional[X] / Union[X, None] down to X.
        for wrapper in ("Optional[", "Union["):
            if text.startswith(wrapper) and text.endswith("]"):
                inner = text[len(wrapper):-1]
                parts = [p.strip() for p in inner.split(",")]
                parts = [p for p in parts if p not in ("None", "NoneType")]
                text = parts[0] if parts else text
                break
        base = text.split("[", 1)[0]
        return base.rsplit(".", 1)[-1].lower()

    origin = getattr(target_type, "__origin__", None)
    if origin is not None:
        return getattr(origin, "__name__", str(origin)).lower()
    return getattr(target_type, "__name__", str(target_type)).lower()


def _coerce(raw: Any, target_type: Any, label: str) -> Any:
    """Best-effort coercion of a YAML/env scalar into the declared type."""
    kind = _type_name(target_type)

    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(f"{label}: cannot read '{raw}' as a boolean")

    if kind == "int":
        if isinstance(raw, bool):
            raise ConfigError(f"{label}: expected an integer, got a boolean")
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{label}: cannot read '{raw}' as an integer") from exc

    if kind == "float":
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{label}: cannot read '{raw}' as a number") from exc

    if kind == "str":
        return str(raw)

    if kind == "decimal":
        try:
            return Decimal(str(raw))
        except ArithmeticError as exc:
            raise ConfigError(f"{label}: cannot read '{raw}' as a decimal") from exc

    if kind == "list":
        if isinstance(raw, (list, tuple)):
            return list(raw)
        # Comma-separated string, which is how env vars express lists.
        return [item.strip() for item in str(raw).split(",") if item.strip()]

    if kind == "dict":
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{label}: cannot read '{raw}' as a mapping") from exc
        if not isinstance(parsed, dict):
            raise ConfigError(f"{label}: expected a mapping")
        return parsed

    return raw


def _set_field(target: Any, attr: str, raw: Any, label: str) -> None:
    declared = {f.name: f.type for f in fields(target)}
    if attr not in declared:
        raise ConfigError(f"unknown config option '{label}'")
    setattr(target, attr, _coerce(raw, declared[attr], label))


def _apply_section(target: Any, values: Dict[str, Any], section_name: str) -> None:
    known = {f.name for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            # Same reasoning as the environment path: a misspelled risk limit
            # that is quietly dropped is worse than a refusal to start.
            raise ConfigError(
                f"unknown config option '{section_name}.{key}'; "
                f"valid options: {sorted(known)}"
            )
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_section(current, value, f"{section_name}.{key}")
            continue
        _set_field(target, key, value, f"{section_name}.{key}")


__all__ = [
    "AdaptiveConfig",
    "ApiConfig",
    "BacktestConfig",
    "Config",
    "ConfigError",
    "ExecutionConfig",
    "KNOWN_STRATEGIES",
    "LoggingConfig",
    "RegimeConfig",
    "RiskConfig",
    "StorageConfig",
    "StrategyConfig",
    "TradingConfig",
]
