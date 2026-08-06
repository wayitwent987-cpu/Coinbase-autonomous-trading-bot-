"""Configuration loading, precedence, coercion and validation."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import yaml

from coinbot.config import Config, ConfigError, RiskConfig, TradingConfig
from coinbot.logging_setup import scrub
from coinbot.models import Granularity


def write_yaml(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


# --------------------------------------------------------------------------- #
# Loading and precedence
# --------------------------------------------------------------------------- #


def test_defaults_are_valid():
    Config().validate()  # must not raise


def test_load_from_yaml(tmp_path):
    path = write_yaml(tmp_path, {
        "trading": {"products": ["ETH-USD"], "granularity": "FIFTEEN_MINUTE"},
        "risk": {"risk_per_trade_pct": 0.01, "max_open_positions": 2},
    })
    cfg = Config.load(path, apply_env=False)
    assert cfg.trading.products == ["ETH-USD"]
    assert cfg.trading.granularity == "FIFTEEN_MINUTE"
    assert cfg.risk.risk_per_trade_pct == 0.01
    assert cfg.risk.max_open_positions == 2
    # Untouched fields keep their defaults.
    assert cfg.risk.max_drawdown_pct == RiskConfig().max_drawdown_pct


def test_missing_file_is_an_error():
    with pytest.raises(ConfigError, match="not found"):
        Config.load("/nonexistent/config.yaml")


def test_env_overrides_yaml(tmp_path):
    path = write_yaml(tmp_path, {"risk": {"risk_per_trade_pct": 0.01}})
    cfg = Config.load(path, apply_env=False)
    assert cfg.risk.risk_per_trade_pct == 0.01
    cfg.apply_env({"COINBOT_RISK_RISK_PER_TRADE_PCT": "0.02"})
    assert cfg.risk.risk_per_trade_pct == 0.02


def test_credential_aliases():
    cfg = Config()
    cfg.apply_env({
        "COINBOT_API_KEY": "organizations/x/apiKeys/y",
        "COINBOT_API_SECRET": "-----BEGIN EC PRIVATE KEY-----abc-----END EC PRIVATE KEY-----",
    })
    assert cfg.api.has_credentials
    assert cfg.api.api_key == "organizations/x/apiKeys/y"


def test_products_from_env_as_csv():
    cfg = Config()
    cfg.apply_env({"COINBOT_PRODUCTS": "BTC-USD, ETH-USD ,SOL-USD"})
    assert cfg.trading.products == ["BTC-USD", "ETH-USD", "SOL-USD"]


def test_unknown_yaml_option_is_rejected(tmp_path):
    """A silently-dropped risk setting is worse than a refusal to start."""
    path = write_yaml(tmp_path, {"trading": {"not_a_real_option": 5}})
    with pytest.raises(ConfigError, match="unknown config option"):
        Config.load(path, apply_env=False)


def test_unknown_env_option_is_rejected():
    cfg = Config()
    with pytest.raises(ConfigError, match="unknown config option"):
        cfg.apply_env({"COINBOT_RISK_TOTALLY_MADE_UP": "1"})


def test_a_misspelled_risk_limit_does_not_pass_silently():
    """The real name is max_drawdown_pct; the typo must not be ignored."""
    cfg = Config()
    with pytest.raises(ConfigError):
        cfg.apply_env({"COINBOT_RISK_MAX_DRAWDOWN": "0.05"})


def test_unrelated_env_vars_are_left_alone():
    cfg = Config()
    cfg.apply_env({"PATH": "/usr/bin", "HOME": "/root", "LANG": "C"})
    assert cfg.risk.max_drawdown_pct == RiskConfig().max_drawdown_pct


# --------------------------------------------------------------------------- #
# Type coercion
# --------------------------------------------------------------------------- #
# `from __future__ import annotations` makes every dataclass field type a
# *string*, so coercion has to resolve names like "List[str]" and "Optional[int]".


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_bool_coercion(raw, expected):
    cfg = Config()
    cfg.apply_env({"COINBOT_TRADING_DRY_RUN": raw})
    assert cfg.trading.dry_run is expected


def test_bad_bool_is_rejected():
    cfg = Config()
    with pytest.raises(ConfigError, match="boolean"):
        cfg.apply_env({"COINBOT_TRADING_DRY_RUN": "maybe"})


def test_numeric_coercion():
    cfg = Config()
    cfg.apply_env({
        "COINBOT_RISK_MAX_OPEN_POSITIONS": "7",
        "COINBOT_RISK_RISK_PER_TRADE_PCT": "0.005",
    })
    assert cfg.risk.max_open_positions == 7
    assert isinstance(cfg.risk.max_open_positions, int)
    assert cfg.risk.risk_per_trade_pct == 0.005


def test_bad_number_is_rejected():
    cfg = Config()
    with pytest.raises(ConfigError, match="integer"):
        cfg.apply_env({"COINBOT_RISK_MAX_OPEN_POSITIONS": "lots"})


def test_dict_coercion_from_json():
    cfg = Config()
    routing = {"TRENDING_UP": {"trend_follow": 1.0}}
    cfg.apply_env({"COINBOT_STRATEGY_ROUTING": json.dumps(routing)})
    assert cfg.strategy.routing == routing


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mutate,message", [
    (lambda c: setattr(c.trading, "products", []), "empty"),
    (lambda c: setattr(c.trading, "products", ["BTCUSD"]), "PRODUCT-QUOTE"),
    (lambda c: setattr(c.trading, "granularity", "TWO_SECOND"), "not supported"),
    (lambda c: setattr(c.risk, "risk_per_trade_pct", 0.5), "risk_per_trade_pct"),
    (lambda c: setattr(c.risk, "risk_per_trade_pct", 0.0), "risk_per_trade_pct"),
    (lambda c: setattr(c.risk, "max_position_pct", 1.5), "max_position_pct"),
    (lambda c: setattr(c.risk, "max_open_positions", 0), "max_open_positions"),
    (lambda c: setattr(c.risk, "max_drawdown_pct", 0.0), "max_drawdown_pct"),
    (lambda c: setattr(c.risk, "atr_stop_multiple", 0.0), "atr_stop_multiple"),
    (lambda c: setattr(c.execution, "order_type", "iceberg"), "order_type"),
    (lambda c: setattr(c.trading, "warmup_bars", 10), "warmup_bars"),
    (lambda c: setattr(c.strategy, "enabled", ["nope"]), "unknown entries"),
])
def test_validation_rejects_bad_settings(mutate, message):
    cfg = Config()
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        cfg.validate()


def test_htf_must_not_be_faster_than_the_trading_timeframe():
    cfg = Config()
    cfg.trading.granularity = "ONE_HOUR"
    cfg.trading.htf_granularity = "FIVE_MINUTE"
    with pytest.raises(ConfigError, match="htf_granularity must be"):
        cfg.validate()


def test_post_only_requires_limit_orders():
    cfg = Config()
    cfg.execution.order_type = "market"
    cfg.execution.post_only = True
    with pytest.raises(ConfigError, match="post_only"):
        cfg.validate()


def test_max_candles_must_cover_warmup():
    cfg = Config()
    cfg.trading.warmup_bars = 500
    cfg.trading.max_candles = 100
    with pytest.raises(ConfigError, match="max_candles"):
        cfg.validate()


def test_all_validation_problems_are_reported_together():
    cfg = Config()
    cfg.trading.products = []
    cfg.risk.max_open_positions = 0
    with pytest.raises(ConfigError) as exc:
        cfg.validate()
    assert "products" in str(exc.value) and "max_open_positions" in str(exc.value)


def test_require_credentials():
    cfg = Config()
    with pytest.raises(ConfigError, match="credentials missing"):
        cfg.require_credentials()


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_secrets_are_redacted_in_output():
    cfg = Config()
    cfg.api.api_key = "organizations/abc/apiKeys/def"
    cfg.api.api_secret = "-----BEGIN EC PRIVATE KEY-----secret-----END EC PRIVATE KEY-----"
    data = cfg.to_dict(redact=True)
    assert data["api"]["api_key"] == "***redacted***"
    assert data["api"]["api_secret"] == "***redacted***"
    # The key *material* must be gone. The field name "api_secret" naturally
    # survives, so check for the value rather than the substring "secret".
    rendered = cfg.to_yaml()
    assert "PRIVATE KEY" not in rendered
    assert "organizations/abc" not in rendered


def test_log_scrubber_removes_key_material():
    pem = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQ\n-----END EC PRIVATE KEY-----"
    assert "MHcCAQ" not in scrub(f"loading key {pem}")
    assert "organizations/" not in scrub("key organizations/abc-123/apiKeys/def-456")
    assert "hunter2" not in scrub('{"api_secret": "hunter2"}')


def test_summary_does_not_leak_credentials():
    cfg = Config()
    cfg.api.api_secret = "super-secret-key-material"
    assert "super-secret" not in cfg.summary()


def test_key_file_loading(tmp_path):
    key_file = tmp_path / "cdp_key.json"
    key_file.write_text(json.dumps({
        "name": "organizations/o/apiKeys/k",
        "privateKey": "-----BEGIN EC PRIVATE KEY-----x-----END EC PRIVATE KEY-----",
    }))
    cfg = Config()
    cfg.api.key_file = str(key_file)
    cfg.api.load_key_file()
    assert cfg.api.api_key == "organizations/o/apiKeys/k"
    assert cfg.api.has_credentials


def test_missing_key_file_is_an_error():
    cfg = Config()
    cfg.api.key_file = "/nonexistent/key.json"
    with pytest.raises(ConfigError, match="key_file not found"):
        cfg.api.load_key_file()


# --------------------------------------------------------------------------- #
# Derived values
# --------------------------------------------------------------------------- #


def test_bar_seconds_matches_granularity():
    cfg = TradingConfig(granularity="FIVE_MINUTE")
    assert cfg.bar_seconds == 300
    assert cfg.granularity_enum is Granularity.FIVE_MINUTE


def test_routing_filters_to_enabled_strategies():
    cfg = Config()
    cfg.strategy.enabled = ["trend_follow"]
    weights = cfg.strategy.weights_for("TRENDING_UP")
    assert set(weights) == {"trend_follow"}


def test_crash_regime_routes_to_nothing():
    """The bot must have no strategy willing to trade a crash."""
    assert Config().strategy.weights_for("CRASH") == {}
