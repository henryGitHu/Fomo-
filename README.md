# Crypto Momentum Scanner

Watches free, public crypto market data and shows you which tokens are
**speeding up**: volume jumping above its normal pace, buyers outnumbering
sellers, and price rising steadily rather than in one spike.

> **It never trades.** It only watches and reports. You decide what to do,
> and you place any trade yourself.

## What works so far (all 6 phases)

| Phase | What it adds | Status |
|---|---|---|
| 1 | Settings, database, DexScreener + GeckoTerminal data, console list of top movers | **Done** |
| 2 | Safety checks (Solana mint/freeze authority, EVM honeypot/tax checks) | **Done** |
| 3 | Combined score + Telegram phone alerts with suggested trades | **Done** |
| 4 | Paper-trade tracking + `report.bat` performance report | **Done** |
| 5 | Reddit mention tracking | **Skipped**: Reddit now requires manual approval for all API access (see below) |
| 6 | Telegram channel mention tracking | **Done** (optional; needs a one-time login) |

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
5. Runs **safety checks** on the highest-momentum tokens that passed the
   filters (details below), and hides any that fail.
6. Works out a **combined score** (momentum + safety margin, plus social
   buzz from phase 5 on) and prints the top tokens in a table.
7. Sends a **phone alert** when a token's combined score reaches 70, with a
   suggested trade. Tokens that failed safety are never alerted.
8. **Paper-trades every alert.** It follows the price for 4 hours and records
   whether the suggested take-profit or stop-loss would have hit first, and
   the result after costs. `report.bat` sums it all up.

---

## Setup (one time, about 5 minutes)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On the first installer screen, **tick "Add python.exe to PATH"**.
2. **Put this folder somewhere permanent**, for example `C:\CryptoScanner`.
3. **Double-click `setup.bat`.** (Run it again after any update that adds
   new features, so newly required packages get installed.) It creates a private Python environment in a
   `.venv` folder, installs what the scanner needs, creates your `.env`
   secrets file, and checks `config.yaml`. Wait for "Setup complete".

That's all the scanner itself needs. Phone alerts need a free Telegram bot;
see **Phone alerts** below.

## Phone alerts (Telegram)

One-time setup, about 2 minutes:

1. In Telegram, open **@BotFather** (blue checkmark) and send `/newbot`. Pick a
   name, then a username ending in `bot`.
2. BotFather replies with a **token** like `123456789:AAH...`. Keep it private.
3. Open your new bot's chat, press **Start**, and send it any message.
4. In your browser, open
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`, with your token in
   place of `YOUR_TOKEN`. Note that it's **bot** followed straight by the token.
   Find `"chat":{"id":` followed by a number. That number is your **chat ID**.
5. Open `.env` in Notepad (right-click, **Open with**, **Notepad**) and fill in:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   TELEGRAM_CHAT_ID=987654321
   ```
6. Double-click **`test-alert.bat`**. You should get a test message on your
   phone. If not, it tells you in plain English what to fix.

Optional Discord: in a channel's settings, go to **Integrations**, then
**Webhooks**, then **New Webhook**. Copy the URL into `.env` as
`DISCORD_WEBHOOK_URL=...` and set `alerts.discord: true` in `config.yaml`.

**What an alert contains:** token, chain, contract address, price, 5m/1h
change, volume, liquidity, market cap, age, buy/sell ratio, social trend,
every safety result, the score breakdown, a risk level (LOW, MEDIUM or HIGH,
with reasons), and a DexScreener link.

**Suggested trade** (a suggestion only; the tool never trades): entry at the
current price, take-profit +15%, stop-loss −8%, and a $50 size. It also
estimates round-trip costs: fees of about 1%, plus slippage estimated from
the pool's liquidity. If those costs would eat more than half the
take-profit gain, the alert warns you.

**When alerts fire:**
- The combined score must reach `alerts.score_threshold` (default 70).
- The token must pass safety. UNVERIFIED tokens can alert, clearly marked,
  unless you set `send_unverified: false`.
- Each token then has a 60-minute cooldown, unless its score jumps 15 or
  more points again.
- At most 3 alerts are sent per scan.

All of these are in the `scoring:`, `alerts:` and `trade:` sections of
`config.yaml`.

## Running it

- **Double-click `run.bat`.** It scans about every 90 seconds and keeps going
  until you close the window or press `Ctrl+C`.
- To do one scan and stop, open a Command Prompt in this folder and type
  `run.bat --once`.
- `run.bat --dry-run` prints alerts in the window instead of sending them to
  your phone. It's handy for trying out new settings. Alerts are still saved
  for the paper-trade report.

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
| Score | Combined score, 0–100. Alerts fire when it reaches your threshold. |
| Mom | Momentum alone, 0–100. Higher means accelerating harder. Hidden first in narrow windows. |
| 5m / 1h | Price change over the last 5 minutes / 1 hour |
| Vol 5m / Vol 1h | Dollar volume traded in the last 5 minutes / 1 hour |
| Vol x | Last 5 minutes of volume compared with its normal pace. `3.9x` means almost 4× faster than usual. |
| Buys | Share of trades in the last 5 minutes that were buys |
| Liq | Money in the trading pool. Low liquidity means big price swings when you trade. |
| MCap | Market cap (or fully-diluted value if market cap isn't known) |
| Age | How long the trading pool has existed |
| Sig | **V** = volume at least 2× normal, **B** = at least 60% buys, **U** = price up over both 5m and 1h, **!** = the whole move came in one giant candle (score cut in half) |
| Safety | **OK** = passed every check, **UNVER** = one or more checks couldn't be done, **FAIL** = failed a check (hidden by default), **-** = not checked yet |

Below the table, **Safety notes** explain in plain words why any listed token
is UNVERIFIED. Then there's a chart link for the #1 token.

If your window is too narrow for every column, the least important ones are
hidden and listed underneath. Make the window wider to see them.

## Safety checks

Safety checks are **hard gates**: a token that fails any of them is never
alerted. If a check can't be finished (a site is down, or has no data on the
token), the token is marked **UNVERIFIED** instead of being quietly passed.
No automated check can catch every scam, so treat OK as "no red flags found",
not "safe".

**Solana tokens** (data from RugCheck; Solana's public server as a backup):
- **Mint authority revoked.** Otherwise the creator can print unlimited new tokens.
- **Freeze authority revoked.** Otherwise the creator can freeze your wallet so you can't sell.
- **LP burned or locked (at least 80%).** Otherwise the creator can pull the
  liquidity. Tokens still on a pump.fun-style bonding curve have no LP to
  pull, so they pass this check.
- **No other RugCheck "danger" warnings**, such as dangerous Token-2022
  features or the token being flagged as already rugged.

**EVM tokens** (Ethereum, Base, BNB Chain, Arbitrum; data from GoPlus, with
honeypot.is as a second opinion):
- **Not a honeypot**, meaning you can actually sell. If either source says
  it's a honeypot, it fails.
- **Buy and sell tax at or below 10%.** When both sources report a tax, the
  higher one is used.
- **Contract source code published**, so it can be checked.
- **No dangerous owner powers:** minting, blacklisting, pausing trading,
  changing taxes, hidden or reclaimable ownership, self-destruct. Powers only
  count while someone still owns the contract. Once ownership is renounced,
  they can't be used.

**All chains:**
- **Top 10 wallets hold 40% or less** of the supply, not counting the trading
  pool itself or burn addresses.

Holder and LP data isn't always available. By default, missing holder or LP
data is noted but doesn't make a token UNVERIFIED. Set `safety.strict_mode:
true` if you want it to.

Each scan checks up to 10 new tokens (`safety.max_checks_per_scan`), highest
momentum first. Results are remembered: 60 minutes for OK, 6 hours for FAIL,
15 minutes for UNVERIFIED. So the first few scans after starting are slower,
while it works through the list.

All thresholds are in the `safety:` section of `config.yaml`.

## Social buzz from Telegram channels (optional)

The scanner can read public Telegram channels and groups that you choose,
and turn mentions into a **social score (0–100)** that feeds the combined
score. It logs in as **your** Telegram account through Telegram's official
API and only reads; it never posts.

**How mentions are counted:**
- **Contract addresses** count fully. A **$TICKER** on its own counts only
  30%, because tickers collide constantly.
- **Shill filtering:** a copy-pasted message counts once however many times
  it's posted, and each person or channel counts once per window.
  (Telegram doesn't reveal account age, so the "new account" filter can't
  apply to Telegram.)
- The score rewards **acceleration** (mentions in the last 30 minutes
  compared with the previous 6 hours) and **breadth** (how many different
  sources). A token that's always talked about doesn't score high just for
  being popular.
- Tokens nobody mentions are scored on momentum and safety alone, so turning
  this on never hides a strong token.

**Setup (about 5 minutes):**
1. Go to <https://my.telegram.org> and log in with your phone number. The
   code arrives in your Telegram app.
2. Click **API development tools** and fill in the form. Any app title and
   short name will do, for example "My Scanner" and "myscanner". Leave the
   URL blank. Click **Create application**.
3. Copy **App api_id** (a number) and **App api_hash** (a long code) into
   `.env`:
   ```
   TELEGRAM_API_ID=1234567
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   ```
   The api_hash is like a password and **can't be reset**, so never share it.
4. In `config.yaml`, under `telegram_channels:`, add the public channels to
   watch (the part after `t.me/`), one per line:
   ```
   channels:
     - somechannel
     - anotherchannel
   ```
5. Double-click **`telegram-login.bat`**. Enter your phone number with its
   country code, then the code Telegram sends you (and your 2-step password,
   if you have one). It then checks that each channel can be read.
6. Set `telegram_channels.enabled: true` and restart `run.bat`. A **Soc**
   column appears in the table, and alerts show the mention trend.

The login is saved in `data\telegram.session`. Anyone with that file can
use your Telegram account, so don't share it. To log out, delete it.

**Reddit (phase 5) is not included.** Since November 2025, Reddit requires
every new API user, even for personal projects, to apply and be approved by
hand, and small projects are often refused. Per this project's rules we
skip sources that need approval rather than work around them. If Reddit
ever approves you, Reddit support can be added.

## Scheduled summaries (Telegram)

On top of instant alerts, the scanner sends regular updates:

- **Hourly digest:** the top 5 tokens right now, with score, safety, 1h/5m
  change, liquidity, risk and a chart link, even if none crossed the alert
  threshold. They're watch-list ideas, not alerts.
- **Paper-trade chart every 2 hours:** an image of today's results. The top
  panel shows your running profit/loss in dollars; the bottom panel shows
  each finished trade as a win (blue) or loss (red). The caption gives net
  P&L, wins and losses, best and worst trade, and the all-time total.
- **End-of-day summary** at 21:00 by your PC's clock.

Change the timing in the `summaries:` section of `config.yaml`. If you want
**only** the hourly digest and no instant alerts, set `alerts.instant: false`.

To get the paper-trade chart right now, run `.\run.bat --send-pnl`. The
latest chart is also saved as `reports\latest-pnl.png`.

## Keeping it running

The scanner only works while `run.bat` is open and the computer is awake.
Alerts, digests and paper trades all pause when it isn't.

**On your laptop** (plugged in):
1. Press Start, type **Power, sleep and battery settings**, and open it.
   Under **Screen, sleep & hibernate timeouts**, set **"When plugged in, put
   my device to sleep after"** to **Never**. The screen turning off is fine.
2. Press Start, type **Control Panel**, and go to **Hardware and Sound**,
   then **Power Options**, then **Choose what closing the lid does**. Set
   **When I close the lid / Plugged in** to **Do nothing**. Now you can
   close the lid and it keeps running.
3. Optional: start it automatically when you log in. See **Start
   automatically with Windows** below.

**24/7 without your laptop:** run it on a small always-on computer instead.
That could be a cheap cloud server (about $4–6/month, and some providers
have free tiers) or a Raspberry Pi at home. It needs a few extra setup
files for Linux, which can be added on request.

## Paper trading and the report

Every alert, including `--dry-run` alerts, is tracked automatically while
`run.bat` is running:
- It records the price at **+5 min, +15 min, +60 min and +4 hours**.
- It simulates the suggested trade. If the price reaches the take-profit
  first, it's a win at +15%. If it reaches the stop-loss first, it's a loss
  at the price seen, which can be worse than −8% if the price gapped down.
  If neither happens within 4 hours, the trade closes at the 4-hour price.
- The estimated fees and slippage from the alert are subtracted, so results
  are **net**.

**Double-click `report.bat`** at any time to see:
- the alert count, win rate, average and median net return, best and worst
  trade, and the total if you had taken every suggestion
- the average price move at +5m, +15m, +60m and +4h
- performance **by chain, by score band, by risk level and by signal**

Options, typed after the name in PowerShell: `.\report.bat --days 7` shows
only the last week, and `.\report.bat --live-only` leaves out dry-run alerts.

**How to use it:** wait for at least 30–50 finished trades. A handful proves
nothing either way. Then look for patterns. For example, if the 70–79 score
band loses but 80+ wins, raise `alerts.score_threshold` to 80. If a chain or
signal keeps losing, turn it down or off.

Honest limits: prices are sampled about every 90 seconds, so very fast
spikes can be missed. Results only build up while `run.bat` is running. And
past results don't guarantee future ones.

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

Secrets such as bot tokens go in **`.env`**, never in `config.yaml`.

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
| GeckoTerminal | Trending pools and new pools, per chain | 30/min published (stricter in practice) | 10/min |
| RugCheck | Solana token safety report | not published | 15/min |
| Solana public RPC | Mint/freeze authority (backup only) | 100 per 10s | 30/min |
| GoPlus | EVM contract security | 30/min | 20/min |
| honeypot.is | EVM buy/sell simulation | not published | 20/min |

A full scan takes roughly 30–60 seconds, and most of that is waiting to stay
under the free rate limits.

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
