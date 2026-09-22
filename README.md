# wnt-nolive-bot

A **paper-only** trading bot for Kalshi's ABC World News Tonight word-mention markets (`KXWORLDNEWSMENTION`).
It tests one idea: **after the cold open, sell YES on the words that have not been said yet.**
No Kalshi key, no signing code, no place-order call. It only reads public Kalshi data.

## v2 rule (replaces the original 1c-97c / 55c version)

Backtested on 40 nights of Kalshi's own public trade history, independent of this bot's own data. The
original rule (qualify 1c-97c, flat sell-YES at 55c) only made money on the subset of words that were
already cheap when it fired -- split out, that subset returned about **+45% ROI**. Everything priced
higher lost money at every cancel time tested, consistently, across most nights. A top-of-scale version
(buying YES on words already likely to be said) was tested two separate ways and found no edge either
way -- a dead end, not built.

## The rules (all of them are settings, see `nolive/config.py`)

1. At **5:32:30 PM Central** it reads every word's price and order book.
2. A word **qualifies** if its YES price is **at or below 30c**. **Counting words** ("3+ times", "5+
   times") are excluded entirely, even if their price would otherwise qualify -- the backtest found
   they behave differently and left them out of every number above.
3. On every qualifying word it sends a paper limit order: **SELL YES at 30c** (same thing as BUY NO at
   70c), **$3 of collateral per word**, computed into contracts.
4. **Hard cap: 50 contracts per word**, regardless of what the dollar math above would otherwise ask
   for. Sizing by dollars alone breaks past this point -- on nights a word is genuinely heading toward
   getting said, real buying volume floods in and fills you to whatever size you request; on quiet
   nights you only get partially filled either way. Past the cap you're structurally overweighting
   losses and underweighting wins. The cap is a config value (`MAX_CONTRACTS_PER_WORD`), easy to raise
   once there's more live data to justify it.
5. The same order is run with **five cancel times**: 5:35, 5:40, 5:45, 5:50, 5:55 PM. The dashboard
   compares them -- 5:55 edged out the others slightly and consistently in backtesting, but margins were
   close enough to confirm on live data before locking it in as the only version.

## How a paper order fills

- **Instant match (taker).** If YES buyers are already bidding 30c or more when the order goes in, it matches right away at *their* price (better than 30c). Kalshi charges a taker fee:
  `fee = round up( 0.07 x contracts x price x (1 - price) )`, rounded up to the cent on every fill.
- **Resting (maker).** What is left waits. It fills only when a YES buyer pays **more than 30c** (a real trade above our price, read from Kalshi's public trade tape). Resting fills pay **no fee** on this series. The bot reads the series' fee type from Kalshi every night and would charge the maker rate if that ever changes.
- One simulation feeds all five versions: a version counts the fills that happened **before its cancel time**.
- Money comes only from **Kalshi's official yes/no result**. SELL YES at price p: word not said = keep p, word said = lose (100 - p).

## Shared database, same idea as the gap bot

Same Supabase project as `wnt-nofade-bot` and `wnt-gap-bot`.

| | |
|---|---|
| **Read from no-fade (SELECT only)** | `days.event_ticker` (tonight's event), `orders.result` (official results it already fetched), `depth` (last book before 5:28, saved for comparison) |
| **Written by this bot** | only tables starting with `nolive_` |
| **Why it records its own data** | no-fade's recorder stops at ~5:28 PM. Prices at 5:32:30, the order book after the cold open, and the trade tape after that exist nowhere else. |

Tables: `nolive_runs` (one row a night), `nolive_markets` (each word's price and book at fire time), `nolive_orders` (one row per word per cancel time), `nolive_fills`, `nolive_trades` (raw tape), `nolive_depth` (book pictures every minute), `nolive_state`, `nolive_activity`.
**The gap is covered.** From 5:28 PM (`RECORD_FROM_CT`) the bot saves an order-book picture of *every* word every 30 seconds, and at the fire it saves every trade since 5:28 for every word. After the fire it keeps a book every minute and every trade on every word (ordered or not) until 5:55. (`nolive_depth.kind` = `pre`, `fire` or `poll`.)
Because the raw tape and books are saved, any other rule can be replayed later without waiting more nights.

## Setup (once)

1. **Supabase**: SQL editor -> paste `sql/001_nolive_schema.sql` -> Run. (The app also runs it at start-up; safe to run twice.)
2. **Streamlit Cloud**: New app -> repo `jovanpost/wnt-nolive-bot`, branch `main`, file `streamlit_app.py`, subdomain `wnt-nolive-bot`. Paste `.streamlit/secrets.toml.example` into Secrets and fill in `DATABASE_URL` (the same one the other bots use) and the two Telegram values.
3. **Keep-alive**: `.github/workflows/keepalive.yml` pings the app every 10 minutes. If your subdomain is different, set a repo variable `APP_URL`.
4. **Telegram**: reuse the gap bot's bot (keep `TELEGRAM_COMMANDS = false`), or make a new bot and set it to `true` for `/nolive_status`, `/nolive_pause`, `/nolive_resume`.

## Checking it

```bash
pip install -r requirements.txt
python3 scripts/live_check.py        # real Kalshi data, read-only: shows who would qualify and a sample book
python3 scripts/offline_test.py      # whole fake night, no network; also runs on Postgres with TEST_DATABASE_URL=...
```

## Migrating from v1

The SQL file now also includes two `alter table` lines that patch an already-deployed database (running
`create table if not exists` alone does nothing to a table that already exists): it renames
`nolive_runs.skip_at_or_above` to `qualify_max_yes_cents` (the meaning flipped -- it's now an at-or-below
cap, not a skip line) and adds `nolive_orders.size_capped`. Re-running `sql/001_nolive_schema.sql` in
Supabase's SQL editor is safe and does both.

## Honest limits

- **Resting fills use a strict rule**: only trades *above* 30c count. A trade exactly at 30c may or may not have reached our order, so it is not counted. This is slightly cautious.
- **Queue position is ignored** (other sellers at 30c who were there first). The book at fire time is saved (`queue_ahead`) so a queue-aware rule can be tested later.
- **Paper orders do not move the market.** Real orders would take liquidity that other people then cannot.
- **Qualifying price = last trade price** (bid, then ask, if there was no trade). A best YES bid above the cap also disqualifies.
- If the app is asleep at 5:32:30 it fires up to 2 minutes late; after that it records the night as `missed` and never fires late.
- A handful of nights is not evidence. The dashboard says so until there are 10 settled nights.
