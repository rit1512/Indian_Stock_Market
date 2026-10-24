---
name: trader-india
description: >-
  Indian Share Market. Live NSE and BSE data for day trading in India: the
  NSE clock in IST (pre-open 9:00, normal market 9:15 to 3:30, closing
  session, the 2026 NSE holidays and Muhurat trading), quotes for any NSE or
  BSE stock in rupees, NIFTY 50, BANK NIFTY, FIN NIFTY, SENSEX, sector
  indices and INDIA VIX with market breadth straight from NSE, top gainers
  and losers for NIFTY, BANK NIFTY, NIFTY NEXT 50 and F&O stocks, a
  15-minute opening-range breakout and breakdown scan of the NIFTY 50, and
  position sizing in rupees with the real intraday costs (brokerage, STT,
  exchange charges, SEBI fee, stamp duty, GST). Use when the user mentions
  NSE, BSE, NIFTY, SENSEX, BANK NIFTY, India VIX, Indian stocks or shares,
  rupees, Zerodha, Kite, Upstox, Groww, intraday or MIS, a ticker like
  RELIANCE, TCS, INFY or HDFCBANK, "is the Indian market open", "top
  gainers today", or "how many shares for a Rs 500 stop loss".
argument-hint: "[clock|quote|indices|movers|orb|size] [symbol]"
---

# trader-india

The Indian share market with free data, read live: NSE's own website feeds
for the market status, the indices and the movers, and Yahoo Finance's chart
endpoint for any NSE (`.NS`) or BSE (`.BO`) stock. Everything is in IST and
rupees. The US skills in this pack (NYSE clock, Alpaca, dollar limits) do not
apply to Indian trades; this skill has its own clock, charges and config.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/india.py" clock --live                       # NSE open? + NSE's own status
python3 "${CLAUDE_SKILL_DIR}/india.py" quote RELIANCE TCS 'M&M' NIFTY     # NSE prices
python3 "${CLAUDE_SKILL_DIR}/india.py" quote RELIANCE --bse --write       # BSE, -> trader/india/prices.json
python3 "${CLAUDE_SKILL_DIR}/india.py" indices                            # indices, VIX, breadth
python3 "${CLAUDE_SKILL_DIR}/india.py" movers --index BANKNIFTY --top 5   # gainers and losers
python3 "${CLAUDE_SKILL_DIR}/india.py" universe --write                   # NIFTY 50 list, cached
python3 "${CLAUDE_SKILL_DIR}/india.py" orb --at 09:45                     # opening-range breaks, NIFTY 50
python3 "${CLAUDE_SKILL_DIR}/india.py" orb SBIN TCS --date 2026-10-23 --at 10:15
python3 "${CLAUDE_SKILL_DIR}/india.py" size --entry 1190 --stop 1183 --target 1204 --max-loss 500
```

Every command takes `--json`. `--now "2026-10-23 09:45"` pins the clock (IST);
`--offline DIR` reads saved files instead of the network. Quote symbols with
`&` in the shell: `'M&M'`. Movers `--index` is one of NIFTY, BANKNIFTY,
NIFTYNEXT50, FOSec (F&O stocks), allSec.

Symbols: plain NSE symbols (`RELIANCE`, `BAJAJ-AUTO`), `--bse` for BSE, or
`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`, `INDIAVIX`. A full Yahoo symbol
(`^NSEI`, `TCS.NS`, `500325.BO`) passes through.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/india/config.json` | capital, max loss, flat-by, your broker's charges (`config.example.json` here) |
| `trader/india/prices.json` | the latest quotes in rupees, from `quote --write` |
| `trader/india/nifty50.csv` | the NIFTY 50 list from NSE, cached by `universe --write` or the first `orb` |

## The data

- **NSE**: `www.nseindia.com/api/marketStatus`, `/api/allIndices`,
  `/api/live-analysis-variations?index=gainers` (and `loosers`, NSE's
  spelling). These are the feeds behind nseindia.com, not an official API.
  NSE drops requests that do not look like a browser, so these calls send a
  browser user agent; on a 401 or 403 the tool loads the home page once for
  its cookies and retries. NSE's per-stock quote API (`/api/quote-equity`)
  answered 403 on 2026-10-24, so stock prices come from Yahoo.
- **Yahoo**: `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`, one
  request per symbol, half a second apart, `query1` on a 429 or a 5xx. A full
  NIFTY 50 scan is 50 requests, about 35 seconds.
- During market hours these prices are close to live, but they are free,
  unofficial and can lag or be cut off. Say so. Your broker's terminal (Kite,
  Upstox, Groww) is the price that fills. Every quote shows its IST time:
  read it out, and say clearly when a price is from the last session.
- If a Mac Python says `CERTIFICATE_VERIFY_FAILED`, run the
  `Install Certificates.command` that ships with python.org Python, or use
  `/usr/bin/python3`.

## The NSE clock

Pre-open 9:00 to 9:08 (orders) and matching until 9:15, normal market 9:15
a.m. to 3:30 p.m. IST, closing session 3:40 to 4:00 p.m. at the closing price.
Closed on weekends and on the 2026 capital-market holidays from NSE's holiday
master (read 2026-10-24): Jan 15, Jan 26, Mar 3, Mar 26, Mar 31, Apr 3, Apr
14, May 1, May 28, Jun 26, Sep 14, Oct 2, Oct 20 (Dussehra), Nov 10, Nov 24,
Dec 25. Muhurat trading falls on Sunday Nov 8 2026: a one-hour evening session,
with the hour set by an NSE circular. The tool does not guess that hour. For
other years it says the holiday list is unknown. `clock --live` also reads
NSE's own status, which wins over the table if they disagree.

Brokers square off intraday (MIS) positions automatically around 3:15 to
3:25 p.m., with a fee. Default `flat_by` is 15:10 so the user closes first.

## Opening-range scan

The range is the high and low of the first 15 minutes (9:15 to 9:30, three
5-minute bars, `--or-minutes` to change). A **breakout** is the first
5-minute bar to close above the range high, a **breakdown** the first to close
below the low. Only bars complete by `--at` count. A stock with any missing
range bar is reported as an error, not guessed. Breakdowns are short setups,
and shorting is off unless `"allow_short": true`.

## Sizing in rupees

`size` finds the most shares where the loss at the stop, charges included,
stays within `--max-loss` (or `max_loss_per_trade_inr`), capped by
`--capital`. It prints the loss at the stop, the profit at the target after
charges, the reward to risk and the move needed just to cover charges.
Equity intraday charges, from zerodha.com/charges (read 2026-10-24):

| charge | rate |
| --- | --- |
| brokerage | 0.03 % or Rs 20 per executed order, whichever is lower |
| STT | 0.025 % on the sell side |
| exchange transaction | NSE 0.00307 %, BSE 0.00375 % |
| SEBI | Rs 10 per crore |
| stamp duty | 0.003 % on the buy side |
| GST | 18 % on brokerage + transaction + SEBI |

Other brokers charge differently: put theirs under `"charges"` in
`trader/india/config.json`. The contract note is the final word. Intraday
leverage (`--leverage 5`) needs `"allow_margin": true` and shows the margin
needed. It never changes the max loss.

## Steps

1. `clock --live`. If the market is closed, say so and say when it opens.
   Anything below then describes the last session: say that too.
2. `indices` for the day's backdrop: NIFTY, BANK NIFTY, breadth, INDIA VIX.
3. `movers` or `orb --at <now>` for candidates. Give each one's numbers, not
   an opinion.
4. For a setup the user picks: stop at the other side of the range (or the
   user's level), target from the user's reward to risk, then `size`.
   Show the shares, rupees at risk, charges and reward to risk.
5. Check it against `trader/india/config.json`: max loss, trades per day,
   `no_entries_after`, `flat_by`. Say PASS or BLOCK and why.
6. The order is the user's to place in their own broker app. If a broker
   connector is set up in Claude (for example Zerodha's Kite MCP), show the
   exact order in plain words (symbol, NSE or BSE, buy or sell, quantity,
   MIS or CNC, limit price, stop-loss) and wait for the user's explicit yes
   for that one order.

## Rules

- Prices are in rupees. Write `Rs` (or the rupee sign) and Indian grouping
  (`Rs 1,00,000`), never dollars.
- Every price carries its IST time. Never call a free quote "real time".
- Lot sizes, F&O, options and expiry-day trading are out of scope here.
- A scan result is a fact about a chart, not a tip.

## What it never does

It never places, changes or cancels an order. It never recommends a stock,
never predicts a price and never claims a quote is live.

## Safety

- Education and tooling, not financial advice. SEBI's study of FY2023 found
  about 70 % of individual intraday equity traders lost money. Say so if the
  user is new.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- No order goes through without the user's explicit yes for that exact order in the chat, plus the
  connector's own approval card. Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in
  the chat.
