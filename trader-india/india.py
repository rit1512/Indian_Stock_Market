#!/usr/bin/env python3
"""The Indian share market (NSE and BSE) for trader-india. Standard library only.

    python3 india.py clock                              # is the NSE open? pre-open, session, next open
    python3 india.py clock --live                       # plus NSE's own market status
    python3 india.py quote RELIANCE TCS NIFTY           # latest prices (NSE by default)
    python3 india.py quote RELIANCE --bse --write       # BSE, -> trader/india/prices.json
    python3 india.py indices                            # NIFTY 50, BANK, sectors, INDIA VIX (NSE live)
    python3 india.py movers --index NIFTY --top 5       # top gainers and losers (NSE live)
    python3 india.py universe --write                   # the NIFTY 50 list -> trader/india/nifty50.csv
    python3 india.py orb --at 09:45                     # 15-minute opening-range breaks, NIFTY 50
    python3 india.py size --entry 1190 --stop 1183 --target 1204 --max-loss 500 --capital 50000

Data (free, unofficial, read one request at a time, half a second apart):
  NSE     https://www.nseindia.com/api/marketStatus, /api/allIndices and
          /api/live-analysis-variations?index=gainers|loosers (NSE's own
          website feeds; they drop non-browser user agents, so these calls
          send a browser one, and on a 401 or 403 they load the home page
          once for its cookies and try again)
  NSE     https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv
  Yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS
          (.NS for NSE, .BO for BSE; query1 on a 429 or a 5xx)
  --offline DIR reads DIR/yahoo-<SYMBOL>.json, DIR/nse-<feed>.json and
  DIR/ind_nifty50list.csv instead (in a file name, ^ and & become _).

Times are India Standard Time (UTC+5:30, no daylight saving). A --now without
an offset is taken as IST.

NSE calendar: the 2026 capital-market trading holidays from NSE's holiday
master (https://www.nseindia.com/api/holiday-master?type=trading, read
2026-10-24). Outside 2026 the clock says the holiday list is unknown.

Charges: the equity intraday rates on https://zerodha.com/charges (read
2026-10-24). Brokers differ: put yours in trader/india/config.json.
"""

import argparse
import csv
import datetime
import gzip
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")   # NSE resets the connection for anything else
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
NSE = "https://www.nseindia.com"
NIFTY50_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
IST = datetime.timedelta(hours=5, minutes=30)
BAR_MIN = 5

NSE_HOLIDAYS = {  # capital market, weekdays only (the Sunday and Saturday ones close nothing)
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
MUHURAT = {  # a one-hour evening session; NSE announces the hour in a circular
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading)",
}
CALENDAR_YEARS = (2026,)
PRE_OPEN = datetime.time(9, 0)      # order entry 9:00 to 9:08, matching to 9:15
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
POST_CLOSE = datetime.time(16, 0)   # closing session 15:40 to 16:00, at the closing price

ALIASES = {  # what people type -> Yahoo's symbol
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX", "VIX": "^INDIAVIX",
}
DISPLAY = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "NIFTY_FIN_SERVICE.NS": "FINNIFTY", "^BSESN": "SENSEX",
           "^INDIAVIX": "INDIAVIX"}
INDEX_ORDER = ["NIFTY 50", "NIFTY BANK", "NIFTY FINANCIAL SERVICES", "NIFTY NEXT 50", "NIFTY MIDCAP 100",
               "NIFTY SMALLCAP 100", "NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY METAL", "NIFTY FMCG",
               "INDIA VIX"]
YAHOO_INDICES = [("NIFTY 50", "^NSEI"), ("NIFTY BANK", "^NSEBANK"), ("SENSEX", "^BSESN"),
                 ("INDIA VIX", "^INDIAVIX")]
MOVER_INDEX = ["NIFTY", "BANKNIFTY", "NIFTYNEXT50", "FOSec", "allSec"]

CHARGES = {  # equity intraday, zerodha.com/charges read 2026-10-24
    "brokerage_pct": 0.03, "brokerage_max": 20.0,   # per executed order, whichever is lower
    "stt_sell_pct": 0.025,
    "txn_pct": {"nse": 0.00307, "bse": 0.00375},
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,                                 # on brokerage + transaction + SEBI
}
DELIVERY = dict(CHARGES, brokerage_pct=0.0, brokerage_max=0.0,   # equity delivery, same page and date
                stt_sell_pct=0.1, stt_buy_pct=0.1, stamp_buy_pct=0.015,
                dp_per_sell=15.34)                               # per scrip sold, GST included


class IndiaError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise IndiaError("--now wants a time like \"2026-10-23 09:45\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt - IST
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ts_to_ist(ts):
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts) + IST


def parse_hhmm(text, flag="--at"):
    m = HHMM.match(str(text or "").strip())
    if not m:
        raise IndiaError("%s wants an IST time like 09:45" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.isoformat() in MUHURAT:
        return False, "Muhurat trading day: " + MUHURAT[d.isoformat()]
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def market_status(now_utc):
    local = to_ist(now_utc)
    d, t = local.date(), local.time()
    ok, reason = trading_day(d)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(), "weekday": local.strftime("%A"),
          "calendar_known": d.year in CALENDAR_YEARS, "open": False, "phase": "closed", "reason": reason,
          "muhurat": MUHURAT.get(d.isoformat())}
    if ok:
        st["opens"], st["closes"] = OPEN.strftime("%H:%M"), CLOSE.strftime("%H:%M")
        if t < PRE_OPEN:
            st["reason"] = "before the pre-open"
        elif t < OPEN:
            st["phase"], st["reason"] = "pre-open", "pre-open session (orders 9:00 to 9:08, matching to 9:15)"
        elif t < CLOSE:
            st["open"], st["phase"], st["reason"] = True, "open", "normal market"
        elif t < POST_CLOSE:
            st["phase"], st["reason"] = "post-close", "closing session (15:40 to 16:00, at the closing price)"
        else:
            st["reason"] = "after the close"
    nd = d if (ok and t < OPEN) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if trading_day(nd)[0]:
            st["next_open"] = "%s 09:15" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- network

_cookies = http.cookiejar.CookieJar()
_urlopen = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies)).open  # tests swap these
_sleep = time.sleep


def _get(url, headers=None, timeout=20):
    """(HTTP status, body). A network failure is an IndiaError."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with _urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return 200, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        raise IndiaError("could not reach %s: %s" % (url.split("/")[2], getattr(e, "reason", e)))


def nse_headers():
    return {"User-Agent": BROWSER_UA, "Accept": "application/json,text/csv,*/*",
            "Accept-Language": "en-US,en;q=0.9", "Referer": NSE + "/"}


def fetch_nse(path):
    """An NSE website feed. On a 401 or 403, load the home page once for its cookies and retry."""
    url = NSE + "/api/" + path
    code, text = _get(url, nse_headers())
    if code in (401, 403):
        _get(NSE + "/", dict(nse_headers(), Accept="text/html"))
        _sleep(PAUSE)
        code, text = _get(url, nse_headers())
    if code != 200:
        raise IndiaError("NSE answered HTTP %d for /api/%s" % (code, path.split("?")[0]))
    try:
        return json.loads(text)
    except ValueError:
        raise IndiaError("NSE sent something that is not JSON for /api/%s" % path.split("?")[0])


def fetch(url):
    """A file from NSE's archives (the NIFTY 50 list, the F&O lot sizes)."""
    code, text = _get(url, nse_headers())
    if code != 200:
        raise IndiaError("NSE answered HTTP %d for %s" % (code, url.rsplit("/", 1)[-1]))
    return text


def yahoo_symbol(sym, bse=False):
    """RELIANCE -> RELIANCE.NS (RELIANCE.BO with bse), NIFTY -> ^NSEI. ^X and X.NS pass through."""
    s = sym.strip().upper()
    if s in ALIASES:
        return ALIASES[s]
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return s + (".BO" if bse else ".NS")


def display(ysym):
    return DISPLAY.get(ysym) or re.sub(r"\.(NS|BO)$", "", ysym)


def stem(ysym):
    return re.sub(r"[^A-Za-z0-9.\-]", "_", ysym)


def yahoo_url(ysym, rng, interval, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=%s&interval=%s" % (
        host, urllib.parse.quote(ysym, safe=""), rng, interval)


def fetch_yahoo(ysym, rng="1d", interval="5m"):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(ysym, rng, interval, host)
        code, text = _get(url)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url)
        if code == 200 or (code == 404 and text.strip().startswith("{")):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise IndiaError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def spark_url(ysyms, rng="1d", interval="1m", host=YAHOO_HOSTS[1]):
    # the trailing _= keeps Yahoo's CDN from answering with a copy a few minutes old
    return "https://%s/v7/finance/spark?symbols=%s&range=%s&interval=%s&_=%d" % (
        host, ",".join(urllib.parse.quote(s, safe="") for s in ysyms), rng, interval, int(time.time() * 1000))


def tick_text(ysyms, offline=None):
    """One small request for the latest price of a few symbols (the dashboard asks every second)."""
    if offline:
        return _offline(offline, "yahoo-spark.json")
    code, text = _get(spark_url(ysyms), timeout=5)
    if code != 200:
        raise IndiaError("Yahoo spark answered HTTP %d" % code)
    return text


def parse_ticks(text):
    """Spark JSON -> {yahoo symbol: {price, prev_close, time_ist, ts}}."""
    try:
        res = json.loads(text)["spark"]["result"] or []
    except (ValueError, KeyError, TypeError):
        raise IndiaError("Yahoo's spark answer is not the shape this tool knows")
    out = {}
    for item in res:
        m = ((item.get("response") or [{}])[0] or {}).get("meta") or {}
        if m.get("regularMarketPrice") is None:
            continue
        ts = m.get("regularMarketTime") or 0
        out[item.get("symbol")] = {"price": float(m["regularMarketPrice"]),
                                   "prev_close": m.get("chartPreviousClose") or m.get("previousClose"),
                                   "ts": ts, "time_ist": ts_to_ist(ts).strftime("%H:%M:%S") if ts else ""}
    return out


FUNDAMENTAL_TYPES = ("trailingPeRatio", "trailingPbRatio", "trailingMarketCap", "annualTotalRevenue",
                     "annualNetIncome", "annualStockholdersEquity", "annualTotalDebt", "annualFreeCashFlow")


def fundamentals_text(ysym, offline=None):
    """Yahoo's fundamentals time series (no crumb needed): P/E, P/B, market cap and annual figures."""
    if offline:
        return _offline(offline, "fundamentals-%s.json" % stem(ysym))
    url = ("https://%s/ws/fundamentals-timeseries/v1/finance/timeseries/%s?type=%s&period1=%d&period2=%d"
           % (YAHOO_HOSTS[0], urllib.parse.quote(ysym, safe=""), ",".join(FUNDAMENTAL_TYPES),
              int(time.time()) - 4 * 365 * 86400, int(time.time()) + 86400))
    code, text = _get(url)
    if code == 429:
        _sleep(BACKOFF)
        code, text = _get(url)
    if code != 200:
        raise IndiaError("Yahoo fundamentals answered HTTP %d for %s" % (code, ysym))
    return text


def _offline(folder, name):
    path = os.path.join(folder, name)
    for p in (path, path + ".gz"):
        if os.path.exists(p):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8") as fh:
                return fh.read()
    raise IndiaError("no offline file %s" % path)


def yahoo_text(ysym, offline=None, rng="1d", interval="5m"):
    if offline:
        return _offline(offline, "yahoo-%s.json" % stem(ysym))
    return fetch_yahoo(ysym, rng, interval)


def offline_name(path):
    """allIndices -> allIndices, ...?index=gainers -> gainers,
    option-chain-v3?type=Indices&symbol=NIFTY&expiry=03-Nov-2026 -> option-chain-v3-NIFTY-03-Nov-2026."""
    base, _, query = path.partition("?")
    q = urllib.parse.parse_qs(query)
    if "index" in q:
        return q["index"][0]
    return "-".join([base] + [q[k][0] for k in ("symbol", "expiry") if k in q])


def nse_doc(path, offline=None):
    """`path` like "allIndices" or "live-analysis-variations?index=gainers"."""
    if offline:
        return json.loads(_offline(offline, "nse-%s.json" % offline_name(path)))
    return fetch_nse(path)


# ---------------------------------------------------------------- parsing

def _chart(text, sym):
    try:
        chart = json.loads(text)["chart"]
    except (ValueError, KeyError, TypeError):
        raise IndiaError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise IndiaError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return res[0]


def parse_quote(text, ysym):
    res = _chart(text, ysym)
    m = res.get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise IndiaError("Yahoo has no price for %s" % ysym)
    t = m.get("regularMarketTime")
    prev = m.get("previousClose") or m.get("chartPreviousClose")
    return {"symbol": display(ysym), "yahoo": ysym, "name": m.get("shortName") or m.get("longName"),
            "exchange": m.get("fullExchangeName") or m.get("exchangeName"), "currency": m.get("currency"),
            "price": float(price), "prev_close": prev, "high": m.get("regularMarketDayHigh"),
            "low": m.get("regularMarketDayLow"), "volume": m.get("regularMarketVolume"),
            "pct": round(100.0 * (price / prev - 1), 2) if prev else None,
            "week52_high": m.get("fiftyTwoWeekHigh"), "week52_low": m.get("fiftyTwoWeekLow"),
            "time_ist": ts_to_ist(t).strftime("%Y-%m-%d %H:%M") if t else "", "source": "yahoo"}


def parse_bars(text, ysym):
    """5-minute bars in IST: [{start, o, h, l, c, v}], skipping empty slots."""
    res = _chart(text, ysym)
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    def col(k, i):
        v = q.get(k) or []
        return v[i] if i < len(v) else None
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        h, l, c = col("high", i), col("low", i), col("close", i)
        if None in (h, l, c):
            continue
        out.append({"start": ts_to_ist(ts), "o": col("open", i), "h": float(h), "l": float(l), "c": float(c),
                    "v": col("volume", i) or 0})
    return out


def parse_nifty50(text):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    syms = [(r.get("Symbol") or "").strip().upper() for r in rows]
    syms = [s for s in syms if s]
    if not syms:
        raise IndiaError("the NIFTY 50 list has no Symbol column")
    return syms


# ---------------------------------------------------------------- the work

def get_quotes(symbols, bse=False, offline=None, pause=PAUSE):
    quotes, errors = [], []
    for i, s in enumerate(symbols):
        ysym = yahoo_symbol(s, bse)
        try:
            if i and pause and not offline:
                _sleep(pause)
            quotes.append(parse_quote(yahoo_text(ysym, offline), ysym))
        except IndiaError as e:
            errors.append(str(e))
    return quotes, errors


def indices_nse(doc):
    by = {r.get("index"): r for r in doc.get("data") or []}
    out = []
    for name in INDEX_ORDER:
        r = by.get(name)
        if r:
            out.append({"index": name, "last": r.get("last"), "change": r.get("variation"),
                        "pct": r.get("percentChange"), "open": r.get("open"), "high": r.get("high"),
                        "low": r.get("low"), "prev_close": r.get("previousClose"),
                        "advances": r.get("advances"), "declines": r.get("declines")})
    return {"as_of": doc.get("timestamp"), "advances": doc.get("advances"), "declines": doc.get("declines"),
            "indices": out, "source": "nse"}


def movers(gainers, losers, index, top):
    def rows(doc):
        return [{"symbol": r.get("symbol"), "ltp": r.get("ltp"), "pct": r.get("perChange"),
                 "prev_close": r.get("prev_price"), "volume": r.get("trade_quantity")}
                for r in ((doc.get(index) or {}).get("data") or [])][:top]
    when = (gainers.get(index) or {}).get("timestamp") or (losers.get(index) or {}).get("timestamp")
    return {"index": index, "as_of": when, "gainers": rows(gainers), "losers": rows(losers), "source": "nse"}


def session_bars(bars, day, cutoff):
    """Bars of `day` on the 5-minute grid from 9:15, complete at `cutoff` (naive IST), before 15:30."""
    out = {}
    for b in bars:
        s = b["start"]
        if s.date() != day or s.second or (s.minute - OPEN.minute) % BAR_MIN:
            continue
        if s.time() < OPEN or s.time() >= CLOSE:
            continue
        if s + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still forming
        out[s] = b
    return [out[k] for k in sorted(out)]


def orb(bars, day, cutoff, or_minutes):
    """The opening range and the first 5-minute CLOSE outside it, or None if the range is incomplete."""
    n = or_minutes // BAR_MIN
    sb = session_bars(bars, day, cutoff)
    first = datetime.datetime.combine(day, OPEN)
    if len(sb) < n or [b["start"] for b in sb[:n]] != [first + datetime.timedelta(minutes=BAR_MIN * k)
                                                         for k in range(n)]:
        return None
    hi = max(b["h"] for b in sb[:n])
    lo = min(b["l"] for b in sb[:n])
    out = {"or_high": round(hi, 2), "or_low": round(lo, 2), "last": round(sb[-1]["c"], 2),
           "signal": "inside", "bars": len(sb)}
    for b in sb[n:]:
        if b["c"] > hi or b["c"] < lo:
            up = b["c"] > hi
            out.update({"signal": "breakout" if up else "breakdown", "price": round(b["c"], 2),
                        "at": (b["start"] + datetime.timedelta(minutes=BAR_MIN)).strftime("%H:%M"),
                        "pct_out": round(100.0 * (b["c"] / (hi if up else lo) - 1), 2)})
            break
    return out


def scan_orb(symbols, day, cutoff, or_minutes, offline=None, pause=PAUSE, min_price=0.0, rng="1d"):
    hits, errors = [], []
    for i, s in enumerate(symbols):
        ysym = yahoo_symbol(s)
        try:
            if i and pause and not offline:
                _sleep(pause)
            r = orb(parse_bars(yahoo_text(ysym, offline, rng), ysym), day, cutoff, or_minutes)
        except IndiaError as e:
            errors.append(str(e))
            continue
        if r is None:
            errors.append("%s: no complete opening range for %s" % (display(ysym), day.isoformat()))
            continue
        if r["last"] < min_price:
            continue
        r["symbol"] = display(ysym)
        hits.append(r)
    return hits, errors


def charges(buy_value, sell_value, exchange="nse", rates=None):
    """Round-trip equity charges in rupees: one buy order and one sell order. Intraday rates by
    default; pass DELIVERY (or your broker's) for a trade held overnight."""
    c = dict(CHARGES)
    c.update(rates or {})
    brok = sum(min(c["brokerage_max"], v * c["brokerage_pct"] / 100) for v in (buy_value, sell_value))
    txn_pct = c["txn_pct"][exchange] if isinstance(c["txn_pct"], dict) else c["txn_pct"]
    txn = (buy_value + sell_value) * txn_pct / 100
    sebi = (buy_value + sell_value) * c["sebi_per_crore"] / 1e7
    stt = sell_value * c["stt_sell_pct"] / 100 + buy_value * c.get("stt_buy_pct", 0.0) / 100
    stamp = buy_value * c["stamp_buy_pct"] / 100
    gst = (brok + txn + sebi) * c["gst_pct"] / 100
    parts = {"brokerage": brok, "stt": stt, "exchange_txn": txn, "sebi": sebi, "stamp_duty": stamp, "gst": gst,
             "dp": c.get("dp_per_sell", 0.0)}
    parts = {k: round(v, 2) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def size(entry, stop, target=None, max_loss=500.0, capital=None, leverage=1.0, exchange="nse", rates=None,
         short=False):
    """Shares so that the loss at the stop, charges included, stays within max_loss."""
    if entry <= 0 or stop <= 0:
        raise IndiaError("entry and stop must be positive")
    if (stop < entry) == short or stop == entry:
        raise IndiaError("a %s needs the stop %s the entry" % ("short" if short else "long",
                                                                "above" if short else "below"))
    if target is not None and (target > entry) == short:
        raise IndiaError("the target is on the wrong side of the entry")
    risk = abs(entry - stop)

    def legs(q, exit_px):
        return (q * exit_px, q * entry) if short else (q * entry, q * exit_px)

    qty = int(max_loss // risk)
    if capital:
        qty = min(qty, int(capital * leverage // entry))
    while qty > 0 and qty * risk + charges(*legs(qty, stop), exchange=exchange, rates=rates)["total"] > max_loss:
        qty -= 1
    if qty <= 0:
        raise IndiaError("not even 1 share fits: %.2f a share at risk against a max loss of Rs %s"
                         % (risk, _n(max_loss)))
    at_stop = charges(*legs(qty, stop), exchange=exchange, rates=rates)
    out = {"side": "short" if short else "long", "qty": qty, "entry": entry, "stop": stop, "target": target,
           "exchange": exchange, "value": round(qty * entry, 2), "risk_per_share": round(risk, 2),
           "loss_at_stop": round(qty * risk + at_stop["total"], 2), "charges_at_stop": at_stop,
           "margin_needed": round(qty * entry / leverage, 2), "leverage": leverage,
           "breakeven_move": round(charges(*legs(qty, entry), exchange=exchange, rates=rates)["total"] / qty, 2)}
    if target is not None:
        at_tgt = charges(*legs(qty, target), exchange=exchange, rates=rates)
        out["charges_at_target"] = at_tgt
        out["profit_at_target"] = round(qty * abs(target - entry) - at_tgt["total"], 2)
        out["reward_risk"] = round(out["profit_at_target"] / out["loss_at_stop"], 2)
    return out


# ---------------------------------------------------------------- options and setups (the dashboard)

FO_LOTS_CSV = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
LOTS = {"NIFTY": 65, "BANKNIFTY": 30}   # fo_mktlots.csv, read 2026-10-27; the CSV wins when it answers
OPTION_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
DELTA = 0.5                             # an at-the-money option moves about half as much as the index
EMA_N = 20
WALL_PCT = 5.0                          # OI walls are looked for within 5 % of spot


def parse_lots(text):
    """fo_mktlots.csv -> {SYMBOL: lot size of the nearest month}."""
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3 or row[1].strip().upper() in ("SYMBOL", ""):
            continue
        try:
            out[row[1].strip().upper()] = int(row[2].strip())
        except ValueError:
            continue
    return out


def _nse_date(text):
    return datetime.datetime.strptime(text, "%d-%b-%Y").date()


def pick_expiry(info, today):
    """The nearest expiry that is not today: an option on its expiry day is a different animal.
    -> (expiry text, the expiry-day text or None)."""
    dates = [d for d in info.get("expiryDates") or [] if _nse_date(d) >= today]
    if not dates:
        raise IndiaError("NSE lists no future expiry")
    if _nse_date(dates[0]) == today and len(dates) > 1:
        return dates[1], dates[0]
    return dates[0], None


def option_chain(doc):
    """option-chain-v3 -> {spot, time, expiry, rows: [{strike, ce, pe}]} with ltp, oi, chg_oi, iv, volume."""
    r = doc.get("records") or {}

    def leg(x):
        x = x or {}
        return {"ltp": x.get("lastPrice") or 0.0, "oi": x.get("openInterest") or 0,
                "chg_oi": x.get("changeinOpenInterest") or 0, "iv": x.get("impliedVolatility") or 0.0,
                "volume": x.get("totalTradedVolume") or 0, "bid": x.get("buyPrice1"), "ask": x.get("sellPrice1")}
    rows = sorted(({"strike": float(d["strikePrice"]), "ce": leg(d.get("CE")), "pe": leg(d.get("PE"))}
                   for d in r.get("data") or [] if d.get("strikePrice") is not None), key=lambda x: x["strike"])
    if not rows:
        raise IndiaError("the option chain is empty")
    exp = next((d.get("expiryDates") for d in r.get("data") or [] if d.get("expiryDates")), None)
    return {"spot": r.get("underlyingValue"), "time": r.get("timestamp"), "expiry": exp, "rows": rows}


def option_stats(chain):
    """ATM strike, put-call ratio of open interest, max pain and the biggest OI walls either side of spot."""
    rows, spot = chain["rows"], chain["spot"]
    atm = min(rows, key=lambda x: abs(x["strike"] - spot))
    ce_oi = sum(x["ce"]["oi"] for x in rows)
    pe_oi = sum(x["pe"]["oi"] for x in rows)

    def pain(k):  # what option buyers would collect if it expired at k
        return sum(x["ce"]["oi"] * max(0.0, k - x["strike"]) + x["pe"]["oi"] * max(0.0, x["strike"] - k)
                   for x in rows)
    near = [x for x in rows if abs(x["strike"] / spot - 1) <= WALL_PCT / 100.0]   # far strikes carry stale OI
    above = [x for x in near if x["strike"] >= spot]
    below = [x for x in near if x["strike"] <= spot]
    return {"atm": atm["strike"], "ce_ltp": atm["ce"]["ltp"], "pe_ltp": atm["pe"]["ltp"],
            "ce_iv": atm["ce"]["iv"], "pe_iv": atm["pe"]["iv"],
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
            "max_pain": min(rows, key=lambda x: pain(x["strike"]))["strike"],
            "call_wall": max(above, key=lambda x: x["ce"]["oi"])["strike"] if above else None,
            "put_wall": max(below, key=lambda x: x["pe"]["oi"])["strike"] if below else None}


def ema(values, n=EMA_N):
    out, k = [], 2.0 / (n + 1)
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def vwap(bars):
    """Volume-weighted average of the typical price; None without volume (Yahoo's indices have none)."""
    vol = sum(b["v"] for b in bars)
    if not vol:
        return None
    return sum((b["h"] + b["l"] + b["c"]) / 3.0 * b["v"] for b in bars) / vol


def session_day(now_ist, or_minutes=15):
    """(day, cutoff) to read: today once the opening range is complete, else the last full session."""
    ok, _ = trading_day(now_ist.date())
    ready = datetime.datetime.combine(now_ist.date(), OPEN) + datetime.timedelta(minutes=or_minutes)
    if ok and now_ist >= ready:
        return now_ist.date(), min(now_ist, datetime.datetime.combine(now_ist.date(), CLOSE))
    d = now_ist.date() - datetime.timedelta(days=1)
    while not trading_day(d)[0]:
        d -= datetime.timedelta(days=1)
    return d, datetime.datetime.combine(d, CLOSE)


def index_trend(bars, day, cutoff, or_minutes=15):
    """Opening range and EMA-20 of 5-minute closes (earlier sessions warm it up) -> a bias.
    bullish: the last close is above the range high and the EMA; bearish: below the low and the EMA."""
    r = orb(bars, day, cutoff, or_minutes)
    if r is None:
        return None
    upto = [b for b in bars if b["start"] + datetime.timedelta(minutes=BAR_MIN) <= cutoff
            and (b["start"].date() < day or b["start"].time() >= OPEN)]
    e = ema([b["c"] for b in upto])[-1]
    last = r["last"]
    if last > r["or_high"] and last > e:
        bias, why = "bullish", "above its opening range high and its 20-bar EMA"
    elif last < r["or_low"] and last < e:
        bias, why = "bearish", "below its opening range low and its 20-bar EMA"
    elif r["or_low"] <= last <= r["or_high"]:
        bias, why = "neutral", "inside its opening range"
    else:
        bias, why = "neutral", "outside the range but on the other side of its 20-bar EMA"
    return dict(r, ema=round(e, 2), bias=bias, why=why)


def option_setup(symbol, trend, stats, lot, max_loss, rules=None, now_t=None):
    """A rule, not a forecast: bullish -> buy the ATM call, bearish -> buy the ATM put, else nothing.
    Stop when the index gets back to the middle of its opening range; target 2x that distance.
    Premiums at the stop and target are estimated with a delta of 0.5."""
    rules = rules or {}
    if not trend or trend["bias"] == "neutral":
        return {"symbol": symbol, "action": "NO TRADE", "check": "WAIT",
                "reasons": ["%s is %s" % (symbol, trend["why"] if trend else "missing its opening range")]}
    up = trend["bias"] == "bullish"
    spot = trend["last"]
    mid = round((trend["or_high"] + trend["or_low"]) / 2.0, 2)
    dist = abs(spot - mid)
    tgt_idx = round(spot + 2 * dist if up else spot - 2 * dist, 2)
    prem = stats["ce_ltp"] if up else stats["pe_ltp"]
    p_stop = max(0.05, round(prem - DELTA * dist, 2))
    p_tgt = round(prem + DELTA * 2 * dist, 2)
    risk_lot = (prem - p_stop) * lot
    lots = int(max_loss // risk_lot) if risk_lot > 0 else 0
    out = {"symbol": symbol, "action": "BUY %s %d %s" % (symbol, int(stats["atm"]), "CE" if up else "PE"),
           "side": "CE" if up else "PE", "strike": stats["atm"], "premium": prem, "lot": lot,
           "cost_per_lot": round(prem * lot, 2), "index_stop": mid, "index_target": tgt_idx,
           "premium_stop": p_stop, "premium_target": p_tgt, "risk_per_lot": round(risk_lot, 2),
           "reward_per_lot": round((p_tgt - prem) * lot, 2), "lots": lots, "why": trend["why"]}
    wall = stats["call_wall"] if up else stats["put_wall"]
    if wall and ((up and wall < tgt_idx) or (not up and wall > tgt_idx)):
        out["wall_note"] = "the biggest %s OI sits at %d, before the target" % ("call" if up else "put", wall)
    reasons = []
    if not rules.get("allow_options"):
        reasons.append("options are off in your config (allow_options)")
    if lots < 1:
        reasons.append("1 lot risks Rs %.0f, over your Rs %s max loss" % (risk_lot, _n(max_loss)))
    if now_t and rules.get("no_entries_after") and now_t > parse_hhmm(rules["no_entries_after"]):
        reasons.append("after %s, no new entries" % rules["no_entries_after"])
    out["check"] = "BLOCK" if reasons else "PASS"
    out["reasons"] = reasons
    return out


def stock_setup(sym, bars, day, cutoff, or_minutes=15, capital=None, max_loss=500.0, rules=None,
                index_bias=None, now_t=None, rates=None):
    """The opening-range plan for one stock, scored 0 to 6. Long only.
    Entry: the last price if the breakout still holds, else a buy-stop at the breakout close.
    Stop: the range low. Target: 2x the risk. Score: +2 holding above the range, +1 above VWAP,
    +1 breakout volume 1.5x the session median, +1 NIFTY bullish, +1 reward to risk after charges >= 1.8."""
    rules = rules or {}
    r = orb(bars, day, cutoff, or_minutes)
    if r is None:
        return None
    out = dict(r, symbol=sym)
    sb = session_bars(bars, day, cutoff)
    if r["signal"] != "breakout":
        out.update(status=r["signal"], score=0, check="SKIP",
                   reasons=["long only" if r["signal"] == "breakdown" else "still inside the range"])
        return out
    k = next(i for i, b in enumerate(sb) if b["start"] + datetime.timedelta(minutes=BAR_MIN)
             == datetime.datetime.combine(day, parse_hhmm(r["at"])))
    stop = r["or_low"]
    first_t = r["price"] + 2 * (r["price"] - stop)
    status = "holding" if r["last"] > r["or_high"] else "faded"
    for b in sb[k + 1:]:
        if b["l"] <= stop:
            status = "stopped"
            break
        if b["h"] >= first_t:
            status = "target hit"
            break
    entry = r["last"] if status == "holding" else r["price"]
    target = round(entry + 2 * (entry - stop), 2)
    vw = vwap(sb)
    vols = sorted(b["v"] for b in sb[:k + 1])
    med = vols[len(vols) // 2] if vols else 0
    vol_x = round(sb[k]["v"] / med, 2) if med else None
    out.update(status=status, entry=round(entry, 2), stop=stop, target=target,
               vwap=round(vw, 2) if vw else None, vol_x=vol_x)
    reasons = []
    try:
        p = size(entry, stop, target, max_loss, capital, 1.0, "nse", rates)
        out.update(qty=p["qty"], value=p["value"], risk=p["loss_at_stop"], charges=p["charges_at_stop"]["total"],
                   reward=p["profit_at_target"], rr=p["reward_risk"])
    except IndiaError as e:
        out.update(qty=0, risk=None, rr=None)
        reasons.append(str(e))
    score = (2 if status == "holding" else 0) + (1 if vw and r["last"] > vw else 0) \
        + (1 if vol_x and vol_x >= 1.5 else 0) + (1 if index_bias == "bullish" else 0) \
        + (1 if out.get("rr") and out["rr"] >= 1.8 else 0)
    out["score"] = score
    if status in ("stopped", "target hit"):
        reasons.append("already %s today" % status)
    if now_t and rules.get("no_entries_after") and now_t > parse_hhmm(rules["no_entries_after"]):
        reasons.append("after %s, no new entries" % rules["no_entries_after"])
    out["check"] = "BLOCK" if reasons else "PASS"
    out["reasons"] = reasons
    return out


def rank_setups(setups, max_trades=3, min_score=3):
    """PASS first, by score then reward to risk; the top max_trades PASS rows scoring min_score or more
    are the picks. A weak PASS is listed but not picked."""
    order = {"PASS": 0, "BLOCK": 1, "SKIP": 2}
    rows = sorted((s for s in setups if s), key=lambda s: (order[s["check"]], -s["score"], -(s.get("rr") or 0)))
    n = 0
    for s in rows:
        s["pick"] = s["check"] == "PASS" and s["score"] >= min_score and n < max_trades
        n += s["pick"]
    return rows


# ---------------------------------------------------------------- files

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if isinstance(doc, str):
            fh.write(doc)
        else:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise IndiaError("%s is not valid JSON: %s" % (path, e))


def write_prices(path, quotes, now_utc):
    old = _read(path, {})
    prices, detail = dict(old.get("prices", {})), dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for q in quotes:
        prices[q["symbol"]] = q["price"]
        detail[q["symbol"]] = {"yahoo": q["yahoo"], "prev_close": q["prev_close"], "time_ist": q["time_ist"],
                               "fetched": stamp}
    _write(path, {"as_of": stamp, "currency": "INR", "source": "yahoo",
                  "note": "Free, unofficial quotes in rupees. Your broker's price is the one that fills.",
                  "prices": prices, "detail": detail})


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rs(x):
    """Rupees with Indian digit grouping: 1234567.5 -> Rs 12,34,567.50."""
    neg, x = x < 0, abs(float(x))
    whole, frac = ("%.2f" % x).split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "%sRs %s.%s" % ("-" if neg else "", ",".join(groups + [tail]), frac)


# ---------------------------------------------------------------- commands

def cmd_clock(a):
    st = market_status(parse_now(a.now))
    live = None
    if a.live:
        doc = nse_doc("marketStatus", a.offline)
        live = next((m for m in doc.get("marketState") or [] if m.get("market") == "Capital Market"), None)
        st["nse"] = live
    if a.json:
        print(json.dumps(st, indent=2))
        return 0 if st["open"] else 1
    print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else st["phase"],
                                       st["reason"]))
    if st.get("opens"):
        print("Today: pre-open 09:00, normal market %s to %s, closing session 15:40 to 16:00."
              % (st["opens"], st["closes"]))
    if st.get("muhurat"):
        print("Muhurat trading today: a one-hour evening session. Check NSE's circular for the hour.")
    if not st["open"] and st.get("next_open"):
        print("Next open: %s IST." % st["next_open"])
    if not st["calendar_known"]:
        print("Holiday list covers %s only: check nseindia.com for this date."
              % " and ".join(map(str, CALENDAR_YEARS)))
    if live:
        print("NSE says: %s (%s), %s %s (%+.2f%%)." % (live.get("marketStatusMessage"), live.get("tradeDate"),
                                                       live.get("index"), live.get("last"),
                                                       float(live.get("percentChange") or 0)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    if not a.symbols:
        raise IndiaError("which symbols? india.py quote RELIANCE TCS NIFTY")
    quotes, errors = get_quotes(a.symbols, a.bse, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, parse_now(a.now))
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
        return 0 if quotes else 1
    for q in quotes:
        chg = (" (%+.2f%% vs prev close %.2f)" % (q["pct"], q["prev_close"])) if q["pct"] is not None else ""
        day = ("  day %.2f to %.2f" % (q["low"], q["high"])) if q["low"] and q["high"] else ""
        print("%-11s %.2f%s%s  %s IST  [%s]" % (q["symbol"], q["price"], chg, day, q["time_ist"], q["exchange"]))
    for e in errors:
        print("error: " + e)
    if a.write and quotes:
        print("Wrote %d price(s) to %s." % (len(quotes), a.write))
    print("Free, unofficial quotes in rupees (via Yahoo). Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_indices(a):
    out, err = None, None
    if a.source == "nse":
        try:
            out = indices_nse(nse_doc("allIndices", a.offline))
        except IndiaError as e:
            err = str(e)
    if out is None:
        quotes, errors = get_quotes([y for _, y in YAHOO_INDICES], offline=a.offline)
        names = dict((y, n) for n, y in YAHOO_INDICES)
        out = {"as_of": quotes[0]["time_ist"] if quotes else None, "source": "yahoo",
               "indices": [{"index": names[q["yahoo"]], "last": q["price"], "pct": q["pct"],
                            "prev_close": q["prev_close"], "high": q["high"], "low": q["low"]} for q in quotes]}
        if err:
            out["nse_error"] = err
        if errors:
            out["errors"] = errors
    if a.json:
        print(json.dumps(out, indent=2))
        return 0 if out["indices"] else 1
    if out.get("nse_error"):
        print("NSE did not answer (%s), so these are from Yahoo." % out["nse_error"])
    print("Indices as of %s (%s)%s:" % (out["as_of"], out["source"].upper(),
                                        (", NSE breadth %s up / %s down" % (out["advances"], out["declines"]))
                                        if out.get("advances") is not None else ""))
    for r in out["indices"]:
        print("  %-26s %10s  %+.2f%%" % (r["index"], _n(r["last"]), float(r["pct"] or 0)))
    return 0 if out["indices"] else 1


def cmd_movers(a):
    m = movers(nse_doc("live-analysis-variations?index=gainers", a.offline),
               nse_doc("live-analysis-variations?index=loosers", a.offline), a.index, a.top)
    if a.json:
        print(json.dumps(m, indent=2))
        return 0
    print("%s movers, NSE, %s:" % (a.index, m["as_of"]))
    for side in ("gainers", "losers"):
        print("  %s:" % side.capitalize())
        for r in m[side] or []:
            print("    %-12s %10.2f  %+.2f%%" % (r["symbol"], float(r["ltp"] or 0), float(r["pct"] or 0)))
        if not m[side]:
            print("    none")
    return 0


def load_universe(a):
    if a.symbols:
        return [s.upper() for s in a.symbols]
    if a.offline:
        return parse_nifty50(_offline(a.offline, "ind_nifty50list.csv"))
    if os.path.exists(a.universe):
        with open(a.universe, encoding="utf-8") as fh:
            return parse_nifty50(fh.read())
    code, text = _get(NIFTY50_CSV, nse_headers())
    if code != 200:
        raise IndiaError("NSE answered HTTP %d for the NIFTY 50 list" % code)
    syms = parse_nifty50(text)
    _write(a.universe, text)
    return syms


def cmd_universe(a):
    if a.offline:
        text = _offline(a.offline, "ind_nifty50list.csv")
    else:
        code, text = _get(NIFTY50_CSV, nse_headers())
        if code != 200:
            raise IndiaError("NSE answered HTTP %d for the NIFTY 50 list" % code)
    syms = parse_nifty50(text)
    if a.write:
        _write(a.write, text)
    print("NIFTY 50: %d symbols. %s" % (len(syms), " ".join(syms)))
    if a.write:
        print("Wrote %s." % a.write)
    return 0


def cmd_orb(a):
    now = to_ist(parse_now(a.now))
    day = datetime.date.fromisoformat(a.date) if a.date else now.date()
    cutoff = datetime.datetime.combine(day, parse_hhmm(a.at)) if a.at else now
    if a.or_minutes % BAR_MIN or a.or_minutes <= 0:
        raise IndiaError("--or-minutes must be a multiple of 5")
    ready = datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=a.or_minutes)
    if cutoff < ready:
        raise IndiaError("the %d-minute opening range is not complete until %s IST"
                         % (a.or_minutes, ready.strftime("%H:%M")))
    ok, why = trading_day(day)
    if not ok:
        raise IndiaError("%s is not a trading day (%s)" % (day.isoformat(), why))
    syms = load_universe(a)
    rng = "1d" if day == now.date() else "1mo"   # Yahoo keeps 5-minute bars for about 60 days
    hits, errors = scan_orb(syms, day, cutoff, a.or_minutes, a.offline, min_price=a.min_price, rng=rng)
    out = {"date": day.isoformat(), "cutoff_ist": cutoff.strftime("%H:%M"), "or_minutes": a.or_minutes,
           "scanned": len(syms), "results": hits, "errors": errors}
    if a.json:
        print(json.dumps(out, indent=2))
        return 0 if hits else 1
    up = [h for h in hits if h["signal"] == "breakout"]
    down = [h for h in hits if h["signal"] == "breakdown"]
    print("Opening-range scan, %s, %d-minute range (09:15 to %s), bars complete by %s IST. %d scanned."
          % (day.isoformat(), a.or_minutes, ready.strftime("%H:%M"), out["cutoff_ist"], len(syms)))
    for title, rows in (("Breakouts (first 5-minute close above the range)", up),
                        ("Breakdowns (first 5-minute close below the range)", down)):
        print("%s: %d" % (title, len(rows)))
        for h in sorted(rows, key=lambda r: -abs(r["pct_out"])):
            print("  %-11s %s at %s  range %s to %s  (%+.2f%%), last %s" % (
                h["symbol"], _n(h["price"]), h["at"], _n(h["or_low"]), _n(h["or_high"]), h["pct_out"],
                _n(h["last"])))
    print("Inside the range: %d." % (len(hits) - len(up) - len(down)))
    for e in errors[:10]:
        print("error: " + e)
    if len(errors) > 10:
        print("(and %d more errors)" % (len(errors) - 10))
    if down:
        print("A breakdown is a short setup. Shorting is off unless trader/india/config.json allows it.")
    return 0 if (up or down) else 1


def cmd_size(a):
    cfg = _read(a.config, {})
    short = bool(a.short)
    if short and not cfg.get("allow_short"):
        raise IndiaError("shorting is off: set \"allow_short\": true in %s first" % a.config)
    lev = a.leverage or 1.0
    if lev > 1 and not cfg.get("allow_margin"):
        raise IndiaError("leverage is off: set \"allow_margin\": true in %s first" % a.config)
    max_loss = a.max_loss if a.max_loss is not None else cfg.get("max_loss_per_trade_inr", 500)
    capital = a.capital if a.capital is not None else cfg.get("capital_inr")
    p = size(a.entry, a.stop, a.target, max_loss, capital, lev, a.exchange, cfg.get("charges"), short)
    if a.json:
        print(json.dumps(p, indent=2))
        return 0
    side = "Sell short" if short else "Buy"
    print("%s %d at %s (%s, %s), stop %s%s." % (side, p["qty"], _n(a.entry), rs(p["value"]), a.exchange.upper(),
                                               _n(a.stop), (", target %s" % _n(a.target)) if a.target else ""))
    print("Loss at the stop: %s (%s a share plus %s charges), max %s."
          % (rs(p["loss_at_stop"]), _n(p["risk_per_share"]), rs(p["charges_at_stop"]["total"]), rs(max_loss)))
    if a.target:
        print("Profit at the target after charges: %s. Reward to risk %.2f."
              % (rs(p["profit_at_target"]), p["reward_risk"]))
    c = p["charges_at_stop"]
    print("Charges at the stop: brokerage %s, STT %s, exchange %s, SEBI %s, stamp %s, GST %s."
          % tuple(rs(c[k]) for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")))
    print("The price has to move %s a share just to cover charges." % _n(p["breakeven_move"]))
    if lev > 1:
        print("Margin needed at %.0fx: %s." % (lev, rs(p["margin_needed"])))
    print("Charges follow the rates in the tool or your config. Your broker's contract note is the final word.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Indian share market, NSE and BSE (trader-india).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read saved yahoo-*/nse-* files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    net(p)
    p.add_argument("--live", action="store_true", help="also ask NSE for its market status")
    p = sub.add_parser("quote", help="latest prices")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--bse", action="store_true", help="BSE prices (.BO) instead of NSE (.NS)")
    p.add_argument("--write", nargs="?", const="trader/india/prices.json", metavar="PATH")
    p = sub.add_parser("indices", help="NIFTY 50, BANK NIFTY, sectors, INDIA VIX")
    net(p)
    p.add_argument("--source", default="nse", choices=["nse", "yahoo"])
    p = sub.add_parser("movers", help="top gainers and losers")
    net(p)
    p.add_argument("--index", default="NIFTY", choices=MOVER_INDEX)
    p.add_argument("--top", type=int, default=10)
    p = sub.add_parser("universe", help="the NIFTY 50 list")
    net(p)
    p.add_argument("--write", nargs="?", const="trader/india/nifty50.csv", metavar="PATH")
    p = sub.add_parser("orb", help="opening-range breakouts and breakdowns")
    net(p)
    p.add_argument("symbols", nargs="*", help="default: the NIFTY 50")
    p.add_argument("--at", metavar="HH:MM", help="bars complete by this IST time (default now)")
    p.add_argument("--date", help="YYYY-MM-DD, default today in IST")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--min-price", type=float, default=0.0)
    p.add_argument("--universe", default="trader/india/nifty50.csv")
    p = sub.add_parser("size", help="shares to buy for a max loss, Indian charges included")
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--target", type=float)
    p.add_argument("--max-loss", type=float, help="rupees, charges included (default config or 500)")
    p.add_argument("--capital", type=float, help="rupees available for this trade")
    p.add_argument("--leverage", type=float, help="intraday margin multiple; needs allow_margin")
    p.add_argument("--exchange", default="nse", choices=["nse", "bse"])
    p.add_argument("--short", action="store_true", help="sell first; needs allow_short")
    p.add_argument("--config", default="trader/india/config.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "indices": cmd_indices, "movers": cmd_movers,
                "universe": cmd_universe, "orb": cmd_orb, "size": cmd_size}[a.cmd](a)
    except IndiaError as e:
        print("india.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
