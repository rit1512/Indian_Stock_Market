#!/usr/bin/env python3
"""Technical, fundamental and market checks for trader-india. Standard library only.

    python3 technicals.py market                  # the market regime: trend, VIX, breadth, FII/DII
    python3 technicals.py swing                   # short-term (2 to 15 day) screen of the NIFTY 50
    python3 technicals.py swing RELIANCE BEL --json

Indicators on daily bars (Yahoo, 1 year): SMA 20 / 50 / 200, RSI 14 (Wilder), MACD 12 / 26 / 9,
ATR 14 (Wilder), 20-day average volume, the prior 55-day high, the 52-week range and the 3-month
return against the NIFTY's.

Fundamentals (Yahoo's fundamentals time series): trailing P/E and P/B, market cap, and from the
last two annual reports revenue growth, profit growth, ROE, debt to equity and free cash flow.
Debt to equity and free cash flow are skipped for banks and financials, where they mean
something else.

Events: NSE's event calendar (https://www.nseindia.com/api/event-calendar): results and board
meetings. A trade held over a results date can gap through its stop.

Every rule is written out below and in SKILL.md. The output is what the rules say, not a forecast.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import india  # noqa: E402

IndiaError = india.IndiaError
FINANCIAL = ("Financial Services",)       # the NIFTY 50 list's Industry column
SWING_RULES = {"swing_max_loss_inr": None, "swing_max_positions": 3, "swing_min_score": 7,
               "results_block_days": 7, "atr_stop": 2.0}


# ---------------------------------------------------------------- indicators

def sma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def rsi(closes, n=14):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    for i in range(n, len(closes)):
        if i > n:
            d = closes[i] - closes[i - 1]
            ag = (ag * (n - 1) + max(0.0, d)) / n
            al = (al * (n - 1) + max(0.0, -d)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    m = [a - b for a, b in zip(india.ema(closes, fast), india.ema(closes, slow))]
    s = india.ema(m, signal)
    return m, s, [a - b for a, b in zip(m, s)]


def atr(bars, n=14):
    """Wilder's average true range."""
    out, prev, a = [], None, None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if prev is None else max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev))
        prev = b["c"]
        if i < n - 1:
            a = (a or 0.0) + tr
            out.append(None)
        elif i == n - 1:
            a = ((a or 0.0) + tr) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


def daily_indicators(bars):
    """Daily bars, oldest first, the last one possibly today's bar in progress -> the numbers the screen reads."""
    if len(bars) < 60:
        raise IndiaError("only %d daily bars, the screen needs 60" % len(bars))
    c = [b["c"] for b in bars]
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    r = rsi(c)
    _, _, h = macd(c)
    a = atr(bars)
    vols = [b["v"] for b in bars]
    prior = bars[-56:-1]
    last = bars[-1]
    yr = bars[-250:]
    out = {"date": last["start"].date().isoformat(), "close": round(c[-1], 2), "prev_close": round(c[-2], 2),
           "sma20": s20[-1], "sma50": s50[-1], "sma200": s200[-1], "rsi": r[-1], "rsi_prev": r[-2],
           "macd_hist": h[-1], "macd_hist_prev": h[-2], "atr": a[-1],
           "volume": vols[-1], "vol_avg20": sum(vols[-21:-1]) / 20.0 if len(vols) > 20 else None,
           "high55": max(b["h"] for b in prior), "low10": min(b["l"] for b in bars[-11:-1]),
           "high52w": max(b["h"] for b in yr), "low52w": min(b["l"] for b in yr),
           "ret_3m": round(100.0 * (c[-1] / c[-64] - 1), 2) if len(c) > 63 else None}
    out["atr_pct"] = round(100.0 * out["atr"] / c[-1], 2) if out["atr"] else None
    for k in ("sma20", "sma50", "sma200", "rsi", "rsi_prev", "macd_hist", "macd_hist_prev", "atr", "vol_avg20"):
        if out[k] is not None:
            out[k] = round(out[k], 2)
    out["trend"] = trend(out)
    return out


def trend(ind):
    """uptrend: close and the 50-day above the 200-day; downtrend: both below; else sideways."""
    if ind["sma200"] is None or ind["sma50"] is None:
        return "unknown"
    if ind["close"] > ind["sma200"] and ind["sma50"] > ind["sma200"]:
        return "uptrend"
    if ind["close"] < ind["sma200"] and ind["sma50"] < ind["sma200"]:
        return "downtrend"
    return "sideways"


# ---------------------------------------------------------------- the market

def market_regime(nifty, vix=None, advances=None, declines=None, pcr=None, flows=None):
    """bullish: NIFTY in an uptrend and above its 50-day; bearish: downtrend and below it; else neutral.
    Risk is high when INDIA VIX is above 20, low below 13."""
    notes = []
    t = nifty["trend"]
    above50 = nifty["sma50"] is not None and nifty["close"] > nifty["sma50"]
    if t == "uptrend" and above50:
        regime = "bullish"
    elif t == "downtrend" and not above50:
        regime = "bearish"
    else:
        regime = "neutral"
    notes.append("NIFTY %s, %s its 50-day (%s) and %s its 200-day (%s)" % (
        t, "above" if above50 else "below", india._n(round(nifty["sma50"] or 0)),
        "above" if nifty["sma200"] and nifty["close"] > nifty["sma200"] else "below",
        india._n(round(nifty["sma200"] or 0))))
    risk = "normal"
    if vix is not None:
        risk = "high" if vix > 20 else "low" if vix < 13 else "normal"
        notes.append("INDIA VIX %.2f (%s risk)" % (vix, risk))
    breadth = None
    if advances is not None and declines is not None and advances + declines:
        breadth = round(100.0 * advances / (advances + declines))
        notes.append("%d%% of NSE stocks up today" % breadth)
    if pcr is not None:
        notes.append("NIFTY put-call ratio %.2f" % pcr)
    for f in flows or []:
        notes.append("%s net Rs %s cr on %s" % (f["category"], f["net"], f["date"]))
    return {"regime": regime, "risk": risk, "breadth": breadth, "nifty_rsi": nifty["rsi"], "notes": notes}


def parse_flows(doc):
    out = []
    for r in doc or []:
        try:
            out.append({"category": r["category"], "date": r["date"], "net": round(float(r["netValue"]), 2)})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def parse_events(doc, symbols=None):
    """NSE event calendar -> {SYMBOL: [(date, purpose)]}, soonest first."""
    out = {}
    for r in doc or []:
        s = (r.get("symbol") or "").upper()
        if not s or (symbols and s not in symbols):
            continue
        try:
            d = datetime.datetime.strptime(r["date"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        out.setdefault(s, []).append((d, r.get("purpose") or ""))
    for v in out.values():
        v.sort()
    return out


def next_event(events, sym, today, days):
    """The first event for sym from today through today + days, or None."""
    for d, purpose in events.get(sym, []):
        if today <= d <= today + datetime.timedelta(days=days):
            return {"date": d.isoformat(), "purpose": purpose, "in_days": (d - today).days}
    return None


# ---------------------------------------------------------------- fundamentals

def parse_fundamentals(text):
    """Yahoo fundamentals time series -> the latest value of each type, plus the one before for annuals."""
    try:
        res = json.loads(text)["timeseries"]["result"] or []
    except (ValueError, KeyError, TypeError):
        raise IndiaError("Yahoo's fundamentals answer is not the shape this tool knows")
    series = {}
    for r in res:
        t = ((r.get("meta") or {}).get("type") or [None])[0]
        pts = [x for x in (r.get(t) or []) if x and x.get("reportedValue")]
        if t and pts:
            series[t] = [(x["asOfDate"], x["reportedValue"]["raw"]) for x in sorted(pts, key=lambda x: x["asOfDate"])]

    def last(t, k=-1):
        v = series.get(t) or []
        return v[k][1] if len(v) >= -k else None

    def growth(t):
        a, b = last(t), last(t, -2)
        return round(100.0 * (a / b - 1), 1) if a is not None and b not in (None, 0) and b > 0 else None
    ni, eq, debt = last("annualNetIncome"), last("annualStockholdersEquity"), last("annualTotalDebt")
    return {"pe": _r(last("trailingPeRatio")), "pb": _r(last("trailingPbRatio")),
            "mcap_cr": round(last("trailingMarketCap") / 1e7) if last("trailingMarketCap") else None,
            "rev_growth": growth("annualTotalRevenue"), "profit_growth": growth("annualNetIncome"),
            "roe": round(100.0 * ni / eq, 1) if ni is not None and eq else None,
            "de": round(debt / eq, 2) if debt is not None and eq else None,
            "fcf_cr": round(last("annualFreeCashFlow") / 1e7) if last("annualFreeCashFlow") is not None else None,
            "profit_cr": round(ni / 1e7) if ni is not None else None,
            "year": (series.get("annualNetIncome") or [("", 0)])[-1][0]}


def _r(x):
    return round(x, 2) if isinstance(x, (int, float)) else None


def fundamental_checks(f, financial=False):
    """Pass / fail checks on the last annual report. Grade: strong when 80 % pass, ok at 50 %, else weak."""
    if not f:
        return {"passed": 0, "of": 0, "grade": "unknown", "notes": ["no fundamentals"]}
    checks = [("revenue growing", f["rev_growth"], lambda v: v > 0),
              ("profit growing", f["profit_growth"], lambda v: v > 0),
              ("ROE 12 % or more", f["roe"], lambda v: v >= 12),
              ("profitable (P/E above 0)", f["pe"] if f["pe"] is not None else f["profit_cr"], lambda v: v > 0)]
    if not financial:
        checks += [("debt to equity 1 or less", f["de"], lambda v: v <= 1),
                   ("free cash flow positive", f["fcf_cr"], lambda v: v > 0)]
    passed, of, notes = 0, 0, []
    for name, v, ok in checks:
        if v is None:
            continue
        of += 1
        if ok(v):
            passed += 1
        else:
            notes.append("not " + name)
    if f["pe"] is not None and f["pe"] > 60:
        notes.append("rich valuation, P/E %.0f" % f["pe"])
    grade = "unknown" if not of else "strong" if passed >= 0.8 * of else "ok" if passed >= 0.5 * of else "weak"
    return {"passed": passed, "of": of, "grade": grade, "notes": notes}


# ---------------------------------------------------------------- short term (2 to 15 days)

def swing_setup(sym, ind, nifty_ret_3m=None, regime=None, fund=None, financial=False, event=None,
                capital=None, max_loss=1000.0, rules=None, price=None):
    """Long only, held 2 to 15 trading days, delivery charges.

    Setup (needs an uptrend and RSI 75 or less):
      breakout  the close is above the prior 55-day high on 1.5x the 20-day average volume
      pullback  the close is within 1 ATR of the 20-day average, RSI 40 to 60, MACD histogram rising
    Plan: entry the last price, stop 2 ATR below, target 2x the risk.
    Score out of 10: technical 6 (uptrend 2, above the 50-day 1, a setup 1, MACD histogram above 0
    or rising 1, 3-month return beating the NIFTY 1), fundamental 3 (3 x the share of checks passed,
    rounded down), market 1 (regime bullish).
    BLOCK: results or a board meeting within results_block_days, or the regime is bearish."""
    rules = dict(SWING_RULES, **(rules or {}))
    entry = round(price or ind["close"], 2)
    up = ind["trend"] == "uptrend"
    rsi_ok = ind["rsi"] is not None and ind["rsi"] <= 75
    breakout = (entry > ind["high55"] and ind["vol_avg20"] and ind["volume"] >= 1.5 * ind["vol_avg20"])
    pullback = (ind["sma20"] is not None and abs(entry - ind["sma20"]) <= ind["atr"]
                and ind["rsi"] is not None and 40 <= ind["rsi"] <= 60
                and ind["macd_hist"] is not None and ind["macd_hist"] > ind["macd_hist_prev"])
    setup = ("breakout" if breakout else "pullback" if pullback else None) if up and rsi_ok else None
    tech = (2 if up else 0) + (1 if ind["sma50"] and entry > ind["sma50"] else 0) + (1 if setup else 0) \
        + (1 if ind["macd_hist"] is not None and (ind["macd_hist"] > 0 or ind["macd_hist"] > ind["macd_hist_prev"]) else 0) \
        + (1 if nifty_ret_3m is not None and ind["ret_3m"] is not None and ind["ret_3m"] > nifty_ret_3m else 0)
    fc = fundamental_checks(fund, financial)
    fscore = int(3.0 * fc["passed"] / fc["of"]) if fc["of"] else 0   # rounded down: half passing is 1, not 2
    mscore = 1 if regime == "bullish" else 0
    out = {"symbol": sym, "setup": setup or "none", "trend": ind["trend"], "close": ind["close"], "entry": entry,
           "rsi": ind["rsi"], "atr": ind["atr"], "atr_pct": ind["atr_pct"], "sma20": ind["sma20"],
           "sma50": ind["sma50"], "sma200": ind["sma200"], "high52w": ind["high52w"], "ret_3m": ind["ret_3m"],
           "vol_x": round(ind["volume"] / ind["vol_avg20"], 2) if ind["vol_avg20"] else None,
           "tech": tech, "fund": fscore, "market": mscore, "score": tech + fscore + mscore,
           "fundamentals": fund, "fund_grade": fc["grade"], "fund_notes": fc["notes"], "event": event}
    reasons, flags = [], list(fc["notes"])
    if not up:
        reasons.append("not in an uptrend (%s)" % ind["trend"])
    elif not rsi_ok:
        reasons.append("RSI %.0f: stretched, no new entry" % (ind["rsi"] or 0))
    elif not setup:
        reasons.append("no breakout or pullback trigger today")
    if ind["atr_pct"] and ind["atr_pct"] > 4:
        flags.append("volatile: ATR %.1f %% of price" % ind["atr_pct"])
    if setup is None or reasons:
        out.update(check="SKIP", reasons=reasons, flags=flags)
        return out
    stop = round(entry - rules["atr_stop"] * ind["atr"], 2)
    target = round(entry + 2 * (entry - stop), 2)
    out.update(stop=stop, target=target)
    if ind["high52w"] and entry < ind["high52w"] < target:
        flags.append("52-week high %s sits below the target" % india._n(ind["high52w"]))
    try:
        p = india.size(entry, stop, target, max_loss, capital, 1.0, "nse", india.DELIVERY)
        out.update(qty=p["qty"], value=p["value"], risk=p["loss_at_stop"], charges=p["charges_at_stop"]["total"],
                   reward=p["profit_at_target"], rr=p["reward_risk"])
    except IndiaError as e:
        out.update(qty=0, risk=None, rr=None)
        reasons.append(str(e))
    if event and event["in_days"] <= rules["results_block_days"]:
        reasons.append("%s on %s, in %d days: gap risk" % (event["purpose"], event["date"], event["in_days"]))
    elif event:
        flags.append("%s on %s" % (event["purpose"], event["date"]))
    if regime == "bearish":
        reasons.append("the market regime is bearish")
    out.update(check="BLOCK" if reasons else "PASS", reasons=reasons, flags=flags)
    return out


def rank_swing(rows, max_positions=3, min_score=7):
    order = {"PASS": 0, "BLOCK": 1, "SKIP": 2}
    rows = sorted(rows, key=lambda r: (order[r["check"]], -r["score"], -(r.get("rr") or 0)))
    n = 0
    for r in rows:
        r["pick"] = r["check"] == "PASS" and r["score"] >= min_score and n < max_positions
        n += r["pick"]
    return rows


# ---------------------------------------------------------------- intraday extras

INTRADAY_MIN_SCORE = 5


def enrich_intraday(row, daily=None, stock_pct=None, nifty_pct=None, rsi5=None, event=None, regime=None):
    """Adds to india.stock_setup's 0 to 6: +1 daily uptrend, +1 beating the NIFTY today (now out of 8).
    BLOCK on a results or board-meeting day. Flags: 5-minute RSI above 80 (chasing), results
    tomorrow, a bearish market regime (a long day trade can still work on a strong day, so it is
    a warning here, where the short-term screen blocks)."""
    if not row or row.get("check") == "SKIP":
        return row
    row = dict(row)
    flags = list(row.get("flags") or [])
    reasons = list(row.get("reasons") or [])
    if daily:
        row["daily_trend"] = daily["trend"]
        row["score"] += 1 if daily["trend"] == "uptrend" else 0
    if stock_pct is not None and nifty_pct is not None:
        row["rs_today"] = round(stock_pct - nifty_pct, 2)
        row["score"] += 1 if row["rs_today"] > 0 else 0
    if rsi5 is not None:
        row["rsi5"] = round(rsi5, 1)
        if rsi5 > 80:
            flags.append("5-min RSI %.0f: chasing" % rsi5)
    if event and event["in_days"] == 0:
        reasons.append("%s today: results-day swings" % event["purpose"])
    elif event and event["in_days"] == 1:
        flags.append("%s tomorrow" % event["purpose"])
    if regime == "bearish":
        flags.append("market regime bearish: a long fights the bigger trend")
    row["flags"] = flags
    row["reasons"] = reasons
    if reasons and row["check"] == "PASS":
        row["check"] = "BLOCK"
    return row


# ---------------------------------------------------------------- command line

def _daily(sym, offline):
    y = india.yahoo_symbol(sym)
    return india.parse_bars(india.yahoo_text(y, offline, "1y", "1d") if not offline
                            else india._offline(offline, "daily-%s.json" % india.stem(y)), y)


def load_market(offline=None):
    nifty = daily_indicators(_daily("NIFTY", offline))
    idx = india.indices_nse(india.nse_doc("allIndices", offline))
    vix = next((r["last"] for r in idx["indices"] if r["index"] == "INDIA VIX"), None)
    try:
        flows = parse_flows(india.nse_doc("fiidiiTradeReact", offline))
    except IndiaError:
        flows = []
    adv, dec = idx.get("advances"), idx.get("declines")
    return nifty, market_regime(nifty, vix, int(adv) if adv is not None else None,
                                int(dec) if dec is not None else None, None, flows)


def cmd_market(a):
    _, m = load_market(a.offline)
    if a.json:
        print(json.dumps(m, indent=2))
        return 0
    print("Market regime: %s, %s risk." % (m["regime"].upper(), m["risk"]))
    for n in m["notes"]:
        print("  " + n)
    return 0


def cmd_swing(a):
    rules = dict(SWING_RULES)
    cfg = india._read(a.config, {})
    rules.update({k: v for k, v in cfg.items() if k in SWING_RULES})
    capital = cfg.get("capital_inr", 50000)
    max_loss = rules["swing_max_loss_inr"] or cfg.get("max_loss_per_trade_inr", 500)
    nifty, m = load_market(a.offline)
    syms = [s.upper() for s in a.symbols]
    industry = {}
    text = india._offline(a.offline, "ind_nifty50list.csv") if a.offline else india.fetch(india.NIFTY50_CSV)
    for r in india.csv.DictReader(india.io.StringIO(text.lstrip("﻿"))):
        industry[(r.get("Symbol") or "").strip().upper()] = (r.get("Industry") or "").strip()
    syms = syms or list(industry)
    try:
        events = parse_events(india.nse_doc("event-calendar", a.offline), set(syms))
    except IndiaError:
        events = {}
    today = india.to_ist(india.parse_now(a.now)).date()
    rows, errors = [], []
    for i, s in enumerate(syms):
        y = india.yahoo_symbol(s)
        try:
            if i and not a.offline:
                india._sleep(india.PAUSE)
            ind = daily_indicators(_daily(s, a.offline))
            fund = None
            try:
                fund = parse_fundamentals(india.fundamentals_text(y, a.offline))
            except IndiaError as e:
                errors.append(str(e))
            rows.append(swing_setup(s, ind, nifty["ret_3m"], m["regime"], fund, industry.get(s) in FINANCIAL,
                                    next_event(events, s, today, 15), capital, max_loss, rules))
        except IndiaError as e:
            errors.append("%s: %s" % (s, e))
    rows = rank_swing(rows, rules["swing_max_positions"], rules["swing_min_score"])
    if a.json:
        print(json.dumps({"market": m, "rows": rows, "errors": errors}, indent=2, default=str))
        return 0
    print("Market regime %s (%s risk). Short-term screen, %d stocks, max loss %s, delivery charges."
          % (m["regime"].upper(), m["risk"], len(rows), india.rs(max_loss)))
    for r in rows[:a.top]:
        if r["check"] == "SKIP":
            print("  %-11s SKIP   score %2d  %s" % (r["symbol"], r["score"], "; ".join(r["reasons"])))
            continue
        print("  %-11s %-5s %s score %2d (%d+%d+%d)  %s: buy %s at %s, stop %s, target %s, risk %s, R:R %s%s"
              % (r["symbol"], r["check"], "*" if r["pick"] else " ", r["score"], r["tech"], r["fund"], r["market"],
                 r["setup"], r.get("qty"), india._n(r["entry"]), india._n(r["stop"]), india._n(r["target"]),
                 india.rs(r["risk"]) if r.get("risk") else "-", r.get("rr"),
                 ("  [" + "; ".join(r["reasons"] + r["flags"]) + "]") if r["reasons"] or r["flags"] else ""))
    for e in errors[:5]:
        print("error: " + e)
    print("Rules, not forecasts. * marks a pick: PASS and score %d or more." % rules["swing_min_score"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Technical, fundamental and market checks (trader-india).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("market", "swing"):
        p = sub.add_parser(name)
        p.add_argument("--offline", metavar="DIR")
        p.add_argument("--now")
        p.add_argument("--json", action="store_true")
        p.add_argument("--config", default="trader/india/config.json")
        if name == "swing":
            p.add_argument("symbols", nargs="*", help="default: the NIFTY 50")
            p.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"market": cmd_market, "swing": cmd_swing}[a.cmd](a)
    except IndiaError as e:
        print("technicals.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
