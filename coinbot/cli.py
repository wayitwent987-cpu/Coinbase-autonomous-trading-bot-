"""Command-line interface.

    coinbot validate                 check config and credentials
    coinbot products                 list tradable Coinbase products
    coinbot regime                   show the current regime per product
    coinbot paper                    trade on live data with simulated money
    coinbot run                      trade for real (requires --i-understand-the-risk)
    coinbot backtest                 replay history through the same loop
    coinbot optimize                 fit parameters on real data, validate out of sample
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
from .optimize.objective import OBJECTIVES
from .optimize.robustness import deflated_sharpe, monte_carlo, returns_from_curve
from .optimize.space import PRESETS, SearchSpace, SpaceError
from .optimize.walkforward import WalkForwardOptimizer, WalkForwardResult
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

    optimize = sub.add_parser(
        "optimize",
        help="fit parameters on historical data and validate them out of sample",
    )
    optimize.add_argument(
        "--space", default="fast",
        help=f"preset ({', '.join(sorted(PRESETS))}) or a YAML/JSON file of "
             f"path -> [values]",
    )
    optimize.add_argument("--list-spaces", action="store_true", help="show the presets and exit")
    optimize.add_argument(
        "--objective", default=None, choices=sorted(OBJECTIVES),
        help="what to rank configurations by (default: risk_adjusted)",
    )
    optimize.add_argument("--folds", type=int, default=4, help="train/test splits")
    optimize.add_argument(
        "--test-fraction", type=float, default=0.25,
        help="share of each fold held back for validation",
    )
    optimize.add_argument(
        "--anchored", action="store_true",
        help="expand the training window from the start instead of rolling it",
    )
    optimize.add_argument(
        "--budget", type=int,
        help="sample this many configurations instead of the full grid",
    )
    optimize.add_argument("--seed", type=int, default=0, help="sampling seed")
    optimize.add_argument("--jobs", type=int, default=1, help="parallel worker processes")
    optimize.add_argument("--data", help="directory of cached candle JSON files")
    optimize.add_argument("--bars", type=int, default=6000, help="bars to download if not cached")
    optimize.add_argument("--equity", type=float, help="starting equity")
    optimize.add_argument(
        "--simulations", type=int, default=2000,
        help="Monte Carlo paths to resample (0 to skip)",
    )
    optimize.add_argument(
        "--write-config", metavar="PATH",
        help="save the validated parameters to a YAML file",
    )
    optimize.add_argument("--json", action="store_true", help="emit machine-readable results")

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


def gather_candles(
    cfg: Config, directory: str, bars: int, *, quiet: bool = False
) -> tuple:
    """Cached candles for every configured product, downloading what is missing.

    Shared by ``backtest`` and ``optimize`` so the two always replay exactly
    the same data — a tuned parameter set validated on one set of bars and
    then backtested on another would be comparing two different experiments.
    """
    candles: Dict[str, List[Candle]] = {}
    htf: Dict[str, List[Candle]] = {}
    client: Optional[CoinbaseClient] = None
    granularity = cfg.trading.granularity

    def say(message: str) -> None:
        if not quiet:
            print(message)

    for product_id in cfg.trading.products:
        cached = load_candles(directory, product_id, granularity)
        if cached:
            candles[product_id] = cached
            htf_cached = load_candles(directory, product_id, cfg.trading.htf_granularity)
            if htf_cached:
                htf[product_id] = htf_cached
            say(f"loaded {len(cached)} cached bars for {product_id}")
            continue

        if client is None:
            client = CoinbaseClient(cfg.api)
        say(f"downloading {bars} bars for {product_id}...")
        try:
            downloaded = client.get_candles_paged(
                product_id, cfg.trading.granularity_enum, bars=bars
            )
        except ExchangeError as exc:
            print(f"  failed: {exc}", file=sys.stderr)
            continue
        if downloaded:
            candles[product_id] = downloaded
            save_candles(directory, product_id, granularity, downloaded)

    if client is not None:
        client.close()
    return candles, htf


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
    candles, htf = gather_candles(cfg, directory, args.bars, quiet=args.json)
    if not candles:
        print("no data to backtest; try 'coinbot fetch' first", file=sys.stderr)
        return 1

    result = Backtester(cfg).run(candles, htf_candles=htf or None, progress_every=500)
    if args.json:
        print(json.dumps(result.metrics.to_dict(), indent=2, default=str))
    else:
        print(result.render())
    return 0


def load_space(spec: str) -> SearchSpace:
    """A preset name, or a YAML/JSON file mapping config paths to value lists."""
    if spec in PRESETS:
        return SearchSpace.preset(spec)

    path = Path(spec).expanduser()
    if not path.exists():
        raise SpaceError(
            f"'{spec}' is neither a preset nor a file. Presets: "
            f"{', '.join(sorted(PRESETS))}"
        )
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise SpaceError("PyYAML is required to read a YAML search space") from None
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return SearchSpace.from_dict(data or {})


def describe_spaces() -> int:
    print("Built-in search spaces:\n")
    for name in sorted(PRESETS):
        space = SearchSpace.preset(name)
        print(f"  {name}  ({space.size:,} combinations)")
        for path, values in sorted(space.dimensions.items()):
            rendered = ", ".join(str(v) for v in values)
            print(f"      {path:<44} {rendered}")
        print()
    print("Or pass a YAML file:  path.to.setting: [value, value, ...]")
    return 0


def nest(values: Dict[str, object]) -> Dict[str, object]:
    """Turn ``{'risk.take_profit_r': 3.0}`` into ``{'risk': {'take_profit_r': 3.0}}``."""
    out: Dict[str, object] = {}
    for path, value in sorted(values.items()):
        cursor = out
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
        cursor[parts[-1]] = value
    return out


def write_tuned_config(path: str, result: WalkForwardResult) -> None:
    """Save the consensus parameters, with the evidence for them in a header.

    Only the tuned keys are written, not a whole config. A partial file layers
    over the defaults exactly like a hand-written one, and it stays readable —
    the operator can see at a glance what the optimizer actually changed.
    """
    try:
        import yaml
    except ImportError:
        raise ConfigError("PyYAML is required to write a config file") from None

    consensus = result.consensus()
    oos = result.oos_metrics
    header = [
        "# Parameters chosen by `coinbot optimize`.",
        "#",
        f"# Objective:        {result.objective}",
        f"# Folds:            {len(result.folds)} "
        f"({result.profitable_folds} profitable out of sample)",
        f"# Configs tried:    {result.total_trials}",
    ]
    if oos is not None:
        header += [
            f"# Out-of-sample:    {oos.total_return:+.2%} return, "
            f"{oos.max_drawdown:.2%} max drawdown, {oos.total_trades} trades",
        ]
    header += [
        "#",
        "# These were fitted on past data. That is not a forecast. Paper-trade",
        "# them before risking anything, and re-run the optimizer periodically",
        "# rather than treating these numbers as settled.",
        "",
    ]
    body = yaml.safe_dump(nest(consensus.values), default_flow_style=False, sort_keys=True)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(header) + body)


def cmd_optimize(cfg: Config, args: argparse.Namespace) -> int:
    if args.list_spaces:
        return describe_spaces()

    if args.equity:
        cfg.backtest.initial_equity = args.equity

    space = load_space(args.space)
    directory = args.data or cfg.backtest.data_dir
    candles, htf = gather_candles(cfg, directory, args.bars, quiet=args.json)
    if not candles:
        print("no data to optimize on; try 'coinbot fetch' first", file=sys.stderr)
        return 1

    optimizer = WalkForwardOptimizer(
        cfg,
        space,
        objective=args.objective,
        folds=args.folds,
        test_fraction=args.test_fraction,
        anchored=args.anchored,
        budget=args.budget,
        seed=args.seed,
        jobs=args.jobs,
    )

    total = len(space.candidates(args.budget, seed=args.seed))
    if not args.json:
        print(
            f"\nSearching {total} configuration(s) over {args.folds} folds. "
            f"This runs {total * args.folds + args.folds * 2} backtests; "
            f"expect it to take a while.\n"
        )

    try:
        result = optimizer.run(
            candles,
            htf_candles=htf or None,
            on_progress=None if args.json else (lambda message: print(f"  {message}")),
        )
    except (ValueError, SpaceError) as exc:
        print(f"optimization failed: {exc}", file=sys.stderr)
        return 1

    payload: Dict[str, object] = result.to_dict()

    # --- Robustness, on the stitched out-of-sample curve only --------- #
    mc = None
    ds = None
    oos_trades = [t for fold in result.folds for t in fold.test_trades]
    if args.simulations > 0 and oos_trades:
        mc = monte_carlo(
            oos_trades,
            starting_equity=cfg.backtest.initial_equity,
            simulations=args.simulations,
            seed=args.seed,
        )
        payload["monte_carlo"] = mc.to_dict()
    if result.oos_equity_curve:
        ds = deflated_sharpe(
            returns_from_curve(result.oos_equity_curve),
            trials=max(1, result.total_trials),
            trial_sharpes=result.trial_sharpes() or None,
        )
        payload["deflated_sharpe"] = ds.to_dict()

    if args.write_config:
        write_tuned_config(args.write_config, result)
        payload["written_to"] = args.write_config

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print()
    print(result.render())
    if mc is not None:
        print()
        print(mc.render())
    if ds is not None:
        print()
        print(ds.render())

    print()
    print(verdict(result, ds))
    if args.write_config:
        print(f"\nWrote the validated parameters to {args.write_config}")
        print("Paper-trade them before using them with real money.")
    return 0


def verdict(result: WalkForwardResult, ds: Optional[object]) -> str:
    """A plain-language read of whether this result is worth acting on.

    Deliberately blunt, and deliberately biased toward "not yet". The failure
    mode this guards against is an operator seeing a positive number, skipping
    the diagnostics above it, and funding the account.
    """
    reasons: List[str] = []
    oos = result.oos_metrics

    if oos is None or oos.total_trades == 0:
        return "VERDICT: no out-of-sample trades. Nothing has been demonstrated."

    if oos.total_return <= 0:
        reasons.append(f"the out-of-sample return is {oos.total_return:.2%}")
    if result.profitable_folds * 2 < len(result.folds):
        reasons.append(
            f"only {result.profitable_folds} of {len(result.folds)} test folds made money"
        )
    if oos.total_trades < 30:
        reasons.append(f"{oos.total_trades} trades is too few to conclude anything")
    if ds is not None and not getattr(ds, "passes", False):
        reasons.append(
            "the Sharpe does not clear the bar set by how many configurations were tried"
        )
    baseline = result.baseline_metrics
    if baseline is not None and oos.total_return <= baseline.total_return:
        reasons.append("the untuned defaults did at least as well")

    if reasons:
        return (
            "VERDICT: do not trade this configuration with real money.\n"
            + "\n".join(f"  - {reason}" for reason in reasons)
            + "\n\n  Fitting harder is not the fix. A search that has to be run"
            "\n  repeatedly until it produces a good number is producing that"
            "\n  number by selection, not by finding an edge."
        )

    return (
        "VERDICT: this configuration survived out-of-sample validation.\n"
        "  That is the minimum bar for consideration, not a green light. Paper-trade\n"
        "  it for weeks and compare the live results against these numbers before\n"
        "  risking anything. If they disagree, believe the live results."
    )


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
    "optimize": cmd_optimize,
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
