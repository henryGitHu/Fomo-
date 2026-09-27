# Crypto Momentum Scanner

Watches free, public crypto market data and shows you which tokens are
**speeding up**: volume jumping above its normal pace, buyers outnumbering
sellers, and price rising steadily rather than in one spike.

> **It never trades.** It only watches and reports. You decide what to do,
> and you place any trade yourself.

## What works so far (phase 1 of 6)

| Phase | What it adds | Status |
|---|---|---|
| 1 | Settings, database, DexScreener + GeckoTerminal data, console list of top movers | **Done** |
| 2 | Safety checks (Solana mint/freeze authority, EVM honeypot/tax checks) | Next |
| 3 | Combined score + Telegram phone alerts with suggested trades | Planned |
| 4 | Paper-trade tracking + `report.bat` performance report | Planned |
| 5 | Reddit mention tracking | Planned |
| 6 | Telegram channel mention tracking | Planned |

Right now, every scan does this:

1. Pulls the tokens that are **trending, boosted, or newly listed** on each chain
   you've turned on (Solana, Ethereum, Base, BNB Chain and Arbitrum by default).
2. Looks up each token's most liquid trading pool and saves a snapshot:
   price, 5m/1h/6h price change, volume, buy/sell counts, liquidity, market
   cap and pool age.
3. Keeps re-checking those tokens for 2 hours, so it learns each one's
   "normal" volume and can spot sudden jumps.
4. Removes anything below the basic filters (default: under $25k liquidity,
   under $50k volume in the last hour, or less than 10 minutes old).
5. Prints the highest-momentum tokens in a table.

---

## Setup (one time, about 5 minutes)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On the first installer screen, **tick "Add python.exe to PATH"**.
2. **Put this folder somewhere permanent**, for example `C:\CryptoScanner`.
3. **Double-click `setup.bat`.** It creates a private Python environment in a
   `.venv` folder, installs what the scanner needs, creates your `.env`
   secrets file, and checks `config.yaml`. Wait for "Setup complete".

That's all phase 1 needs. You don't need any accounts or API keys.

## Running it

- **Double-click `run.bat`.** It scans about every 90 seconds and keeps going
  until you close the window or press `Ctrl+C`.
- To do one scan and stop, open a Command Prompt in this folder and type
  `run.bat --once`.
- `run.bat --dry-run` prints alerts in the window instead of sending them to
  your phone. This matters from phase 3 onward, when alerts exist.

If the internet or a data source goes down, the scanner logs the problem and
tries again on the next scan. It won't crash.

### Reading the table

```
 Score   Token    Chain            Price       5m       1h   Vol 5m   Vol 1h   Vol x   Buys     Liq    MCap   Age   Sig
    85   ROCKET   Solana       $0.004210    +6.2%   +18.5%     $42k    $160k    3.9x    72%   $210k   $3.8M    5h   VBU
```
(This row is made-up sample data, shown only to explain the columns.)

| Column | Meaning |
|---|---|
| Score | Momentum, 0–100. Higher means accelerating harder. |
| 5m / 1h | Price change over the last 5 minutes / 1 hour |
| Vol 5m / Vol 1h | Dollar volume traded in the last 5 minutes / 1 hour |
| Vol x | Last 5 minutes of volume compared with its normal pace. `3.9x` means almost 4× faster than usual. |
| Buys | Share of trades in the last 5 minutes that were buys |
| Liq | Money in the trading pool. Low liquidity means big price swings when you trade. |
| MCap | Market cap (or fully-diluted value if market cap isn't known) |
| Age | How long the trading pool has existed |
| Sig | **V** = volume at least 2× normal, **B** = at least 60% buys, **U** = price up over both 5m and 1h, **!** = the whole move came in one giant candle (score cut in half) |

Under the table is a chart link for the #1 token.

**Momentum isn't safety.** Until phase 2 is done, nothing here has been
checked for rugs or honeypots. Don't trade from this list yet.

## Changing settings

Every setting is in **`config.yaml`**, and each one has a plain-English
comment explaining it. Open the file in Notepad, change a value, save it,
then restart `run.bat`.

Common changes:

- **Stop watching a chain:** set its `enabled:` to `false`.
- **Add a chain:** copy one of the chain blocks and fill in the IDs that
  DexScreener and GeckoTerminal use in their web addresses. For example,
  Polygon is `polygon` on DexScreener and `polygon_pos` on GeckoTerminal,
  with `type: evm`.
- **Stricter or looser filters:** change the values under `filters:`.
- **Show more rows:** change `display.top_n`.

If you make a mistake in the file, the scanner stops at startup and tells
you which setting to fix, in plain English.

Secrets such as bot tokens go in **`.env`**, never in `config.yaml`. Phase 1
doesn't use any secrets.

## Where things are stored

- `data/scanner.db` holds all snapshots. It's a SQLite file; DB Browser for
  SQLite can open it if you want to look inside. Snapshots older than 14 days
  are deleted automatically.
- `logs/scanner.log` records what happened and any errors. When the log gets
  large it rotates, and only a few old copies are kept.

## Data sources and limits

Both sources are free, need no key, and are used only through their official
public APIs. The scanner spaces out its requests so it stays under each
limit, and backs off if a source asks it to slow down.

| Source | What we use | Published limit | We use |
|---|---|---|---|
| DexScreener | Boosted tokens, new token profiles, pair data | 60/min (lists), 300/min (pairs) | 50 and 250/min |
| GeckoTerminal | Trending pools and new pools, per chain | 30/min | 25/min |

A full scan takes roughly 30 seconds, and most of that is waiting to stay
under GeckoTerminal's limit.

## Start automatically with Windows (optional)

1. Press Start, type **Task Scheduler**, and open it.
2. Click **Create Basic Task...** and name it "Crypto Scanner".
3. For the trigger, choose **When I log on**.
4. For the action, choose **Start a program**. Browse to `run.bat` in this
   folder, and set **Start in** to this folder, for example `C:\CryptoScanner`.
5. Finish. The scanner window will open every time you log in.

## For the curious: running the tests

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```
