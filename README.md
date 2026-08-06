# Coinbase Trading Bot

An autonomous trading bot for **Coinbase Advanced Trade**. It classifies the
market into one of six conditions and routes to strategies suited to each one,
sizes every position off an explicit risk budget, and stops itself when losses
mount.

Runs on macOS, Linux and Windows. Python only — no compilers, no numpy.

---

## ⚠️ Read this before anything else

**This software can lose money. Possibly all of it.**

- **No one can promise a trading bot is profitable — including this one.** It
  has not been validated on real market data. Everything in this repository was
  tested on *synthetic* price data, which proves the code is correct, not that
  the strategy makes money.
- **A backtest is not a prediction.** Strategies tuned on past data routinely
  fail on future data. Fees and slippage are charged in the simulator precisely
  because ignoring them is how bots look profitable and trade unprofitably.
- **"Maximum profit in the shortest time" and "safe" are opposites.** Faster
  and bigger means larger losses just as readily as larger gains. The defaults
  here are deliberately conservative (0.75% of equity risked per trade).
- **Run it in paper mode for weeks before risking a cent.** Then start with an
  amount you would be genuinely fine losing.
- This is not financial advice, and it is not affiliated with or endorsed by
  Coinbase.

---

## What it does

Every bar, the bot:

1. Pulls fresh candles and computes ~20 indicators in one pass.
2. **Classifies the market condition** into one of six regimes.
3. **Routes to the strategies that suit that regime** and combines their votes.
4. **Sizes the position** as the smallest of several independent caps.
5. Works a limit order, chasing then crossing the spread if it must.
6. Saves state so a restart resumes instead of starting over.

### Market conditions and how it responds

| Condition | What it looks like | What the bot does |
|---|---|---|
| `TRENDING_UP` | Higher highs, ADX rising, price above the slow EMA | Trend-following and breakout entries |
| `TRENDING_DOWN` | The mirror image | Same, short side (off by default on spot) |
| `RANGING` | No trend, contained volatility | Buys the floor, sells the ceiling |
| `VOLATILE_CHOP` | Wide swings, no direction | Trades very little — this tape eats bots |
| `SQUEEZE` | Volatility collapsed, bands compressed | Waits for the coil to release, then joins |
| `CRASH` | Fast, deep drop, large relative to normal volatility | **Stands aside. No new positions.** |

The crash rule is two-sided on purpose: the move must be large in absolute
terms *and* large relative to that asset's own volatility. A fixed −7% alone
would flag routine noise on a coin that swings 5% a bar.

### Risk controls that can stop the bot

| Control | Default | Effect |
|---|---|---|
| Risk per trade | 0.75% of equity | Position size is derived from the stop distance |
| Max position | 25% of equity | No single bet dominates |
| Max drawdown | 15% | **Halts trading. Does not self-clear.** |
| Daily loss limit | 3% | Halts until the next day |
| Consecutive losses | 6 | Halts and cools down |
| Correlation cap | 0.85 | BTC and ETH together count as one bet |

Stops only ever move in your favour. A stop that can loosen is not a stop.

---

## Setup (about 10 minutes)

### 1. Install Python

macOS ships with Python but often an old one. Check:

```bash
python3 --version
```

If that is below 3.9, install a current version from
[python.org/downloads](https://www.python.org/downloads/) and reopen Terminal.

### 2. Download and install the bot

Open **Terminal** (press `Cmd+Space`, type "Terminal") and run these one at a
time:

```bash
git clone https://github.com/wayitwent987-cpu/Coinbase-autonomous-trading-bot-.git
cd Coinbase-autonomous-trading-bot-
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The `source .venv/bin/activate` line has to be run **every time you open a new
Terminal window** before using the bot. You will know it worked because your
prompt gains a `(.venv)` prefix.

### 3. Create your Coinbase API key

1. Sign in to Coinbase and go to
   **[the CDP portal](https://portal.cdp.coinbase.com/access/api)**.
2. Create a new API key.
3. Give it **View** and **Trade** permissions. **Do not give it Transfer or
   Withdraw** — the bot never needs to move money off the exchange, and a key
   that cannot withdraw cannot be used to drain your account.
4. If Coinbase offers an IP allowlist, restrict the key to your own IP.
5. Download the JSON key file. **You only get one chance to see the private
   key.** Keep the file somewhere safe and never share it or commit it.

### 4. Give the bot your key

In Terminal, replace the placeholder text with your real values:

```bash
export COINBOT_API_KEY="organizations/YOUR-ORG-ID/apiKeys/YOUR-KEY-ID"
export COINBOT_API_SECRET="-----BEGIN EC PRIVATE KEY-----
YOUR KEY CONTENTS HERE
-----END EC PRIVATE KEY-----"
```

Or, more simply, point the bot at the file you downloaded:

```bash
export COINBOT_KEY_FILE="/Users/you/Downloads/cdp_api_key.json"
```

These only last for the current Terminal window. To make them permanent, add
the lines to `~/.zshrc`.

### 5. Make your config

```bash
cp config/config.example.yaml config/config.yaml
```

Open `config/config.yaml` in any text editor. Every setting is commented. You
can leave it all as-is to start.

### 6. Check that it works

```bash
coinbot validate
```

This confirms your key works, lists your products, and shows your balance. It
places no orders. If it fails, the message tells you what is wrong.

---

## Using it

Run these with your virtual environment active (`source .venv/bin/activate`).

```bash
coinbot validate      # check config and credentials
coinbot products      # list tradable markets
coinbot regime        # what condition is the market in right now?

coinbot fetch         # download historical candles for backtesting
coinbot backtest      # replay history through the real trading loop

coinbot paper         # trade live prices with fake money  <-- START HERE
coinbot status        # positions, trades, P&L

coinbot run --i-understand-the-risk    # real money
```

### The order you should do this in

1. **`coinbot fetch`** then **`coinbot backtest`** — see how the strategy would
   have behaved on real history. Look at max drawdown, not just return.
2. **`coinbot paper`** — leave it running for at least a few weeks. This is the
   step people skip and regret.
3. Compare paper results to the backtest. If they disagree badly, the backtest
   was optimistic. Do not proceed.
4. Only then consider `coinbot run`, starting small.

`coinbot run` refuses to start without `--i-understand-the-risk`. That is
intentional friction.

To stop the bot, press `Ctrl+C`. **Open positions are not closed automatically**
— the bot will not panic-sell on shutdown. Close them yourself in the Coinbase
app if you want to be flat.

---

## If something goes wrong

**The bot halted and will not trade.**
Check `coinbot status`. A drawdown halt is deliberate and does not clear
itself — that is the point. Review what happened before restarting.

**It says "not enough history".**
Run `coinbot fetch` first, or lower `warmup_bars` in your config.

**`coinbot: command not found`.**
Your virtual environment is not active. Run `source .venv/bin/activate`, or use
`python3 -m coinbot.cli` instead.

**Authentication fails.**
Usually the key was pasted with the line breaks stripped, or your computer's
clock is wrong (the signature is time-sensitive).

---

## Configuration

Settings come from three places, later ones winning:

1. Built-in defaults
2. `config/config.yaml`
3. Environment variables (`COINBOT_RISK_RISK_PER_TRADE_PCT=0.01`)

A misspelled option is a **startup error**, not a warning. A risk limit you
think you changed but did not is worse than a bot that refuses to start.

---

## For developers

```bash
pip install -r requirements-dev.txt
pytest -q
```

Architecture:

```
Coinbase API ──> exchange/     REST + JWT auth + WebSocket
                    │
                 data/         candle buffer -> one indicator pass per bar
                    │
                 regime/       six-state classifier with hysteresis
                    │
                 strategies/   5 strategies + regime router
                    │
                 risk/         sizing, exposure caps, circuit breakers
                    │
                 execution/    Broker interface: live | paper | backtest
                    │
                 engine/       the loop      storage/  SQLite state
```

The seam that matters is `execution/broker.py`. Backtest, paper and live all
implement one interface, so the same strategy and risk code runs in all three.
The backtester drives the real `TradingBot.run_once()` rather than a
reimplementation, which is the only way to be sure a backtest reflects live
behaviour.

---

## Honest limitations

- **Not validated on real market data.** The test suite proves the code does
  what it says on synthetic tapes with known properties. It does not show the
  strategy has an edge. You must run your own backtests on real history.
- **Backtest fills are assumptions.** Orders fill at the next bar's open, and
  a resting limit only fills if price traded *through* it, not merely touched
  it. These are deliberately conservative but they are still assumptions.
- **Spot is long-only.** In a downtrend the bot mostly sits in cash.
- **No slow-indicator support beyond EMA200** without raising `max_candles`.
- **Strategy parameters were tuned on synthetic data**, so treat them as
  starting points, not optimized values.
