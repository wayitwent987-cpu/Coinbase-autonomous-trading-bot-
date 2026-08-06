"""Command-line interface.

    coinbot validate                 check config and credentials
    coinbot products                 list tradable Coinbase products
    coinbot regime                   show the current regime per product
    coinbot paper                    trade on live data with simulated money
    coinbot run                      trade for real (requires --i-understand-the-risk)
    coinbot backtest                 replay history through the same loop
    coinbot fetch                    download candles for offline backtesting
    coinbot status                   report saved state from the database
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .backtest.engine import Backtester
from .config import Config, ConfigError
from .data.features import FeatureParams, compute_features
from .engine.bot import TradingBot
from .exchange.errors import ExchangeError
from .exchange.rest import CoinbaseClient
from .execution.live import LiveBroker
from .execution.paper import PaperBroker
from .logging_setup import setup_logging
from .models import Candle, Granularity
from .regime.detector import RegimeDetector
from .storage.state import StateStore
from .util.numeric import to_decimal
from .util.timing import iso


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinbot",
        description="Regime-aware autonomous trading bot for Coinbase Advanced Trade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="path to a YAML config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "-p", "--products", help="comma-separated product ids, overriding the config"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check configuration and credentials")
    sub.add_parser("products", help="list tradable products")

    regime = sub.add_parser("regime", help="show the current market regime")
    regime.add_argument("--bars", type=int, default=400, help="history to analyze")

    paper = sub.add_parser("paper", help="trade live data with simulated money")
    paper.add_argument("--equity", type=float, default=10_000.0, help="starting balance")
    paper.add_argument("--iterations", type=int, help="stop after N loops")

    run = sub.add_parser("run", help="trade with real money")
    run.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        help="required acknowledgement that this places real orders",
    )
    run.add_argument("--iterations", type=int, help="stop after N loops")

    backtest = sub.add_parser("backtest", help="replay historical data")
    backtest.add_argument("--data", help="directory of cached candle JSON files")
    backtest.add_argument("--bars", type=int, default=3000, help="bars to download if not cached")
    backtest.add_argument("--equity", type=float, help="starting equity")
    backtest.add_argument("--json", action="store_true", help="emit machine-readable results")

    fetch = sub.add_parser("fetch", help="download candles for offline backtesting")
    fetch.add_argument("--bars", type=int, default=3000)
    fetch.add_argument("--out", default="data", help="output directory")

    status = sub.add_parser("status", help="report saved state")
    status.add_argument("--trades", type=int, default=10, help="recent trades to show")

    return parser


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.products:
        cfg.trading.products = [p.strip() for p in args.products.split(",") if p.strip()]
        cfg.validate()
    return cfg


def candle_path(directory: str, product_id: str, granularity: str) -> Path:
    return Path(directory) / f"{product_id}_{granularity}.json"


def save_candles(directory: str, product_id: str, granularity: str, candles: List[Candle]) -> Path:
    path = candle_path(directory, product_id, granularity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "start": c.start, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close, "volume": c.volume,
                }
                for c in candles
            ]
        )
    )
    return path


def load_candles(directory: str, product_id: str, granularity: str) -> Optional[List[Candle]]:
    path = candle_path(directory, product_id, granularity)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return [
        Candle(
            start=int(r["start"]), open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]), volume=float(r.get("volume", 0)),
        )
        for r in raw
    ]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_validate(cfg: Config, args: argparse.Namespace) -> int:
    print("Configuration")
    print("-" * 60)
    print(cfg.summary())
    print()

    if not cfg.api.has_credentials:
        print("  credentials: NOT CONFIGURED")
        print("    Set COINBOT_API_KEY and COINBOT_API_SECRET, or point")
        print("    api.key_file at a CDP key JSON file.")
        print("    (backtesting works without credentials)")
        return 1

    print("  credentials: present")
    client = CoinbaseClient(cfg.api)
    if not client.ping():
        print("  connection:  FAILED — check the key, its permissions, and the clock")
        return 1
    print("  connection:  OK")

    try:
        products = client.get_products()
    except ExchangeError as exc:
        print(f"  products:    FAILED ({exc})")
        return 1

    missing = [p for p in cfg.trading.products if p not in products]
    for product_id in cfg.trading.products:
        product = products.get(product_id)
        if product is None:
            print(f"  {product_id}: NOT FOUND")
        else:
            flags = []
            if not product.tradable:
                flags.append("NOT TRADABLE")
            if product.limit_only:
                flags.append("limit-only")
            print(
                f"  {product_id}: ok  base_inc={product.base_increment} "
                f"price_inc={product.price_increment} "
                f"min={product.min_base_size or product.min_quote_size} "
                f"{' '.join(flags)}"
            )

    try:
        fees = client.get_fee_rates()
        print(f"\n  fee tier: maker {fees['maker']:.3%}  taker {fees['taker']:.3%}")
    except ExchangeError:
        pass

    account = client.get_accounts()
    quote = account.get(cfg.trading.quote_currency, {})
    print(
        f"  balance: {quote.get('available', Decimal(0))} {cfg.trading.quote_currency} "
        f"available"
    )
    client.close()
    return 1 if missing else 0


def cmd_products(cfg: Config, args: argparse.Namespace) -> int:
    client = CoinbaseClient(cfg.api)
    products = client.get_products()
    quote = cfg.trading.quote_currency
    matching = sorted(
        (p for p in products.values() if p.quote_currency == quote and p.tradable),
        key=lambda p: p.product_id,
    )
    print(f"{len(matching)} tradable {quote} products:\n")
    for product in matching:
        print(
            f"  {product.product_id:<16} base_inc={str(product.base_increment):<12} "
            f"price_inc={product.price_increment}"
        )
    client.close()
    return 0


def cmd_regime(cfg: Config, args: argparse.Namespace) -> int:
    client = CoinbaseClient(cfg.api)
    detector = RegimeDetector(cfg.regime)
    params = FeatureParams.from_regime_config(cfg.regime, 105_120)

    print(f"{'product':<12} {'price':>12} {'regime':<15} {'conf':>5}  detail")
    print("-" * 78)
    for product_id in cfg.trading.products:
        try:
            candles = client.get_candles_paged(
                product_id, cfg.trading.granularity_enum, bars=args.bars
            )
        except ExchangeError as exc:
            print(f"{product_id:<12} error: {exc}")
            continue
        if len(candles) < params.min_bars:
            print(f"{product_id:<12} not enough history ({len(candles)} bars)")
            continue

        features = compute_features(product_id, candles, params)
        if features is None:
            print(f"{product_id:<12} could not compute features")
            continue
        # Replay the tail so hysteresis settles rather than reporting bar one.
        for i in range(max(0, len(candles) - 5), len(candles)):
            snapshot = compute_features(product_id, candles[: i + 1], params)
            if snapshot is not None:
                state = detector.detect(snapshot)
        print(
            f"{product_id:<12} {features.close:>12,.4f} {state.regime.value:<15} "
            f"{state.confidence:>5.2f}  {state.detail}"
        )
    client.close()
    return 0


def cmd_paper(cfg: Config, args: argparse.Namespace) -> int:
    cfg.trading.dry_run = True
    # Market data only — the PaperBroker in front of it owns the money, so no
    # order ever reaches the exchange even though the feed is live.
    market = LiveBroker(CoinbaseClient(cfg.api), cfg.api, cfg.trading)
    broker = PaperBroker(
        starting_cash=to_decimal(args.equity),
        quote_currency=cfg.trading.quote_currency,
        data_source=market,
        exec_cfg=cfg.execution,
    )
    store = StateStore(cfg.storage.db_path)
    bot = TradingBot(cfg, broker, store=store)
    print(f"Paper trading with {args.equity} simulated {cfg.trading.quote_currency}.")
    print("Market data is live; no real orders are placed. Ctrl-C to stop.\n")
    bot.run(max_iterations=args.iterations)
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if not args.i_understand_the_risk:
        print(
            "Refusing to start live trading without --i-understand-the-risk.\n"
            "\n"
            "This command places real orders with real money and can lose it.\n"
            "Run 'coinbot paper' first and review the results before going live.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cfg.trading.dry_run = False
    broker = LiveBroker(api_cfg=cfg.api, trading_cfg=cfg.trading)

    # Use the account's real fee tier so sizing and backtests agree with
    # what the exchange actually charges.
    rates = broker.fee_rates()
    cfg.execution.taker_fee_bps = rates["taker"] * 10_000
    cfg.execution.maker_fee_bps = rates["maker"] * 10_000

    store = StateStore(cfg.storage.db_path)
    bot = TradingBot(cfg, broker, store=store)

    print("*** LIVE TRADING — REAL MONEY ***")
    print(cfg.summary())
    print("Ctrl-C stops the loop. Open positions are NOT auto-closed on exit.\n")
    bot.run(max_iterations=args.iterations)
    return 0


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    if args.equity:
        cfg.backtest.initial_equity = args.equity
    directory = args.data or cfg.backtest.data_dir
    granularity = cfg.trading.granularity

    candles: Dict[str, List[Candle]] = {}
    htf: Dict[str, List[Candle]] = {}
    client: Optional[CoinbaseClient] = None

    for product_id in cfg.trading.products:
        cached = load_candles(directory, product_id, granularity)
        if cached:
            candles[product_id] = cached
            htf_cached = load_candles(directory, product_id, cfg.trading.htf_granularity)
            if htf_cached:
                htf[product_id] = htf_cached
            print(f"loaded {len(cached)} cached bars for {product_id}")
            continue

        if client is None:
            client = CoinbaseClient(cfg.api)
        print(f"downloading {args.bars} bars for {product_id}...")
        try:
            downloaded = client.get_candles_paged(
                product_id, cfg.trading.granularity_enum, bars=args.bars
            )
        except ExchangeError as exc:
            print(f"  failed: {exc}", file=sys.stderr)
            continue
        if downloaded:
            candles[product_id] = downloaded
            save_candles(directory, product_id, granularity, downloaded)

    if client is not None:
        client.close()
    if not candles:
        print("no data to backtest; try 'coinbot fetch' first", file=sys.stderr)
        return 1

    result = Backtester(cfg).run(candles, htf_candles=htf or None, progress_every=500)
    if args.json:
        print(json.dumps(result.metrics.to_dict(), indent=2, default=str))
    else:
        print(result.render())
    return 0


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    client = CoinbaseClient(cfg.api)
    for product_id in cfg.trading.products:
        for granularity in (cfg.trading.granularity_enum, cfg.trading.htf_granularity_enum):
            print(f"fetching {args.bars} x {granularity.value} for {product_id}...")
            try:
                candles = client.get_candles_paged(product_id, granularity, bars=args.bars)
            except ExchangeError as exc:
                print(f"  failed: {exc}", file=sys.stderr)
                continue
            if not candles:
                print("  no data returned")
                continue
            path = save_candles(args.out, product_id, granularity.value, candles)
            print(
                f"  saved {len(candles)} bars -> {path} "
                f"({iso(candles[0].start)} .. {iso(candles[-1].start)})"
            )
    client.close()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(cfg.storage.db_path)
    if not path.exists():
        print(f"no state database at {path}")
        return 1

    store = StateStore(cfg.storage.db_path)
    positions = store.load_positions()
    trades = store.load_trades(limit=args.trades)
    curve = store.load_equity_curve(limit=2)

    print(f"State: {path}")
    print("-" * 66)
    if curve:
        print(f"  last equity: {curve[-1][1]:,.2f} {cfg.trading.quote_currency}")
    peak = store.get_meta("peak_equity")
    if peak:
        print(f"  peak equity: {Decimal(str(peak)):,.2f}")
    halt = store.get_meta("halt_reason")
    if halt:
        print(f"  HALTED: {halt}")
    last_loop = store.get_meta("last_loop")
    if last_loop:
        print(f"  last loop:   {iso(float(last_loop))}")

    print(f"\n  Open positions ({len(positions)}):")
    if not positions:
        print("    none")
    for position in positions.values():
        print(
            f"    {position.product_id:<12} size={position.size} "
            f"entry={position.entry_price} stop={position.stop_price} "
            f"strategy={position.strategy or '?'} bars={position.bars_held}"
        )

    print(f"\n  Recent trades ({len(trades)} of {store.trade_count()}):")
    for trade in trades:
        print(
            f"    {iso(trade.closed_at)}  {trade.product_id:<10} "
            f"{trade.side.value:<4} pnl={float(trade.pnl):+9.2f} "
            f"net={float(trade.net_pnl):+9.2f} [{trade.strategy}] {trade.exit_reason}"
        )
    store.close()
    return 0


COMMANDS = {
    "validate": cmd_validate,
    "products": cmd_products,
    "regime": cmd_regime,
    "paper": cmd_paper,
    "run": cmd_run,
    "backtest": cmd_backtest,
    "fetch": cmd_fetch,
    "status": cmd_status,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.logging, verbose=args.verbose)

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces this
        parser.error(f"unknown command {args.command}")

    try:
        return handler(cfg, args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ExchangeError as exc:
        print(f"exchange error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
