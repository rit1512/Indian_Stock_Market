#!/usr/bin/env python3
"""A live dashboard for trader-india, on your own computer. Standard library only.

    python3 dashboard.py                      # http://127.0.0.1:8765, opens your browser
    python3 dashboard.py --port 9000 --no-open
    python3 dashboard.py --offline ../tests/fixtures/trader-india/live-2026-10-27 --now "2026-10-27 12:10"

Four background loops read the same free feeds as india.py and technicals.py:
  every 1 s    the NIFTY and BANKNIFTY price (one small Yahoo request; slows to 30 s if Yahoo
               pushes back, and comes back to 1 s when it answers again)
  every 30 s   NSE market status, indices, movers, FII/DII flows, NIFTY and BANKNIFTY
               5-minute bars and their option chains (the nearest expiry that is not today)
  every 180 s  5-minute bars for each NIFTY 50 stock (the intraday screen)
  every 30 min daily bars for a year, fundamentals and NSE's event calendar (the short-term
               screen), after the first intraday scan so the two do not pile up on Yahoo
Every request is one at a time per loop, half a second apart. The page asks for the price every
second and for everything else every 30 seconds. Setups are worked out on each request, so
changing capital or max loss on the page re-sizes them at once.

It listens on 127.0.0.1 only. It places no orders: there is no order code in it.
"""

import argparse
import csv
import datetime
import io
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import india  # noqa: E402
import technicals as tech  # noqa: E402

TICK, FAST, SLOW, DAILY = 1, 30, 180, 1800
TICK_MAX = 30
DEFAULT_RULES = {"capital_inr": 50000, "max_loss_per_trade_inr": 500, "max_trades_per_day": 3,
                 "no_entries_after": "14:45", "flat_by": "15:10", "allow_options": False}
DEFAULT_RULES.update(tech.SWING_RULES)


class Hub(object):
    """Everything the loops have read, behind one lock."""

    def __init__(self, offline=None, now=None, config=None):
        self.offline, self.fixed_now, self.config = offline, now, config
        self.lock = threading.Lock()
        self.data = {"errors": {}, "bars": {}, "daily": {}, "fund": {}, "scan": {"done": 0, "total": 0, "at": None},
                     "daily_scan": {"done": 0, "total": 0, "at": None}, "tick": {"interval": TICK}}
        self.lots = dict(india.LOTS)
        self.scanned = threading.Event()

    def now_ist(self):
        return india.to_ist(india.parse_now(self.fixed_now))

    def rules(self):
        r = dict(DEFAULT_RULES)
        r.update(india._read(self.config, {}) if self.config else {})
        return r

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def err(self, key, e):
        with self.lock:
            self.data["errors"][key] = str(e)

    def ok(self, key):
        with self.lock:
            self.data["errors"].pop(key, None)

    def _try(self, key, fn):
        try:
            v = fn()
            self.ok(key)
            return v
        except india.IndiaError as e:
            self.err(key, e)
        except Exception as e:  # a feed changed shape: keep the dashboard up, say what broke
            self.err(key, "%s: %s" % (type(e).__name__, e))
            traceback.print_exc()
        return None

    def _pause(self):
        if not self.offline:
            time.sleep(india.PAUSE)

    # ------------------------------------------------------------ the loops

    def tick(self):
        """One request for both index prices. Returns the seconds to wait before the next one."""
        t = self._try("tick", lambda: india.parse_ticks(india.tick_text(list(india.OPTION_INDEX.values()),
                                                                        self.offline)))
        with self.lock:
            cur = self.data["tick"]
            if t:
                old = cur
                cur = {"interval": TICK, "fetched": self.now_ist().strftime("%H:%M:%S")}
                for sym, y in india.OPTION_INDEX.items():
                    keep = old.get(sym)
                    # a cached answer can be older than what we have: never step back in time
                    cur[sym] = t[y] if y in t and (not keep or t[y]["ts"] >= keep["ts"]) else keep
            else:
                cur = dict(cur, interval=min(TICK_MAX, cur.get("interval", TICK) * 2))
            self.data["tick"] = cur
            return cur["interval"]

    def fast(self):
        off = self.offline
        now = self.now_ist()
        st = india.market_status(india.parse_now(self.fixed_now))
        st["nse"] = self._try("status", lambda: next((m for m in india.nse_doc("marketStatus", off).get("marketState")
                                                       or [] if m.get("market") == "Capital Market"), None))
        self.put("status", st)
        v = self._try("indices", lambda: india.indices_nse(india.nse_doc("allIndices", off)))
        if v:
            self.put("indices", v)
        g = self._try("movers", lambda: (india.nse_doc("live-analysis-variations?index=gainers", off),
                                         india.nse_doc("live-analysis-variations?index=loosers", off)))
        if g:
            self.put("movers", {k: india.movers(g[0], g[1], k, 8) for k in ("NIFTY", "BANKNIFTY")})
        f = self._try("flows", lambda: tech.parse_flows(india.nse_doc("fiidiiTradeReact", off)))
        if f is not None:
            self.put("flows", f)
        if not off:
            lots = self._try("lots", lambda: india.parse_lots(india.fetch(india.FO_LOTS_CSV)))
            if lots:
                self.lots.update({k: v for k, v in lots.items() if k in india.LOTS})
        for sym, ysym in india.OPTION_INDEX.items():
            bars = self._try("bars-" + sym, lambda: india.parse_bars(india.yahoo_text(ysym, off, "5d"), ysym))
            if bars:
                with self.lock:
                    self.data["bars"][sym] = bars

            def chain(sym=sym):
                exp, today_exp = india.pick_expiry(
                    india.nse_doc("option-chain-contract-info?symbol=%s" % sym, off), now.date())
                c = india.option_chain(india.nse_doc(
                    "option-chain-v3?type=Indices&symbol=%s&expiry=%s" % (sym, exp), off))
                c["expiry_today"] = today_exp
                return c
            c = self._try("chain-" + sym, chain)
            if c:
                with self.lock:
                    self.data.setdefault("chains", {})[sym] = c
            self._pause()

    def universe(self):
        """The NIFTY 50 symbols and their industries."""
        text = india._offline(self.offline, "ind_nifty50list.csv") if self.offline else india.fetch(india.NIFTY50_CSV)
        rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
        return [(r.get("Symbol") or "").strip().upper() for r in rows if r.get("Symbol")], \
            dict(((r.get("Symbol") or "").strip().upper(), (r.get("Industry") or "").strip()) for r in rows)

    def slow(self):
        off = self.offline
        u = self._try("universe", self.universe)
        if not u:
            return
        syms, industry = u
        with self.lock:
            self.data["scan"] = {"done": 0, "total": len(syms), "at": self.data["scan"].get("at")}
            self.data["universe"], self.data["industry"] = syms, industry   # rows appear as bars arrive
        for i, s in enumerate(syms):
            ysym = india.yahoo_symbol(s)
            bars = self._try("bars-" + s, lambda: india.parse_bars(india.yahoo_text(ysym, off, "5d"), ysym))
            with self.lock:
                if bars:
                    self.data["bars"][s] = bars
                self.data["scan"]["done"] = i + 1
            self._pause()
        with self.lock:
            self.data["scan"]["at"] = self.now_ist().strftime("%H:%M:%S")
        self.scanned.set()

    def daily(self):
        off = self.offline
        self.scanned.wait()
        with self.lock:
            syms = list(self.data.get("universe") or [])
            have_fund = dict(self.data["fund"])
        ev = self._try("events", lambda: tech.parse_events(india.nse_doc("event-calendar", off), set(syms)))
        if ev is not None:
            self.put("events", ev)
        todo = ["NIFTY"] + syms
        with self.lock:
            self.data["daily_scan"] = {"done": 0, "total": len(todo), "at": self.data["daily_scan"].get("at")}
        today = self.now_ist().date().isoformat()
        for i, s in enumerate(todo):
            y = india.yahoo_symbol(s)
            bars = self._try("daily-" + s, lambda: india.parse_bars(
                india._offline(off, "daily-%s.json" % india.stem(y)) if off else india.yahoo_text(y, None, "1y", "1d"), y))
            if s != "NIFTY" and (have_fund.get(s) or {}).get("_day") != today:
                f = self._try("fund-" + s, lambda: tech.parse_fundamentals(india.fundamentals_text(y, off)))
                if f:
                    f["_day"] = today
                    with self.lock:
                        self.data["fund"][s] = f
                self._pause()
            with self.lock:
                if bars:
                    self.data["daily"][s] = bars
                self.data["daily_scan"]["done"] = i + 1
            self._pause()
        with self.lock:
            self.data["daily_scan"]["at"] = self.now_ist().strftime("%H:%M:%S")

    def loop(self, fn, every):
        while True:
            t = time.time()
            wait = self._try("loop", fn)
            if self.offline:
                return
            time.sleep(max(0.2, (wait if isinstance(wait, (int, float)) else every) - (time.time() - t)))

    # ------------------------------------------------------------ what the page gets

    def state(self, capital=None, max_loss=None):
        rules = self.rules()
        capital = capital or rules["capital_inr"]
        max_loss = max_loss or rules["max_loss_per_trade_inr"]
        swing_loss = rules.get("swing_max_loss_inr") or max_loss
        now = self.now_ist()
        day, cutoff = india.session_day(now)
        live = day == now.date()
        now_t = now.time() if live else None
        with self.lock:
            d = json.loads(json.dumps({k: v for k, v in self.data.items()
                                       if k not in ("bars", "chains", "daily", "fund", "events")}, default=str))
            bars, daily = dict(self.data["bars"]), dict(self.data["daily"])
            fund, events = dict(self.data["fund"]), dict(self.data.get("events") or {})
            chains = dict(self.data.get("chains", {}))
            syms = list(self.data.get("universe") or [])
            industry = dict(self.data.get("industry") or {})
        trends, options = {}, {}
        for sym in india.OPTION_INDEX:
            t = india.index_trend(bars[sym], day, cutoff) if sym in bars else None
            trends[sym] = t
            c = chains.get(sym)
            if c:
                st = india.option_stats(c)
                near = [r for r in c["rows"] if abs(r["strike"] - st["atm"]) <= 8 * (50 if sym == "NIFTY" else 100)]
                options[sym] = {"stats": st, "expiry": c["expiry"], "expiry_today": c.get("expiry_today"),
                                "time": c["time"], "spot": c["spot"], "lot": self.lots.get(sym), "chain": near,
                                "setup": india.option_setup(sym, t, st, self.lots.get(sym, 1), max_loss, rules,
                                                            now_t)}
        # the market
        ind = {}
        for s, b in daily.items():
            try:
                ind[s] = tech.daily_indicators(b)
            except india.IndiaError:
                pass
        regime = None
        if "NIFTY" in ind:
            idx = d.get("indices") or {}
            vix = next((r["last"] for r in idx.get("indices") or [] if r["index"] == "INDIA VIX"), None)
            adv, dec = idx.get("advances"), idx.get("declines")
            regime = tech.market_regime(ind["NIFTY"], vix, int(adv) if adv is not None else None,
                                        int(dec) if dec is not None else None,
                                        (options.get("NIFTY") or {}).get("stats", {}).get("pcr"), d.get("flows"))
        reg = (regime or {}).get("regime")
        # intraday
        nifty_pct = _pct_today(bars.get("NIFTY"), day, cutoff)
        nifty_bias = (trends.get("NIFTY") or {}).get("bias")
        intraday = []
        for s in syms:
            if s not in bars:
                continue
            row = india.stock_setup(s, bars[s], day, cutoff, 15, capital, max_loss, rules, nifty_bias, now_t,
                                    rules.get("charges"))
            closes = [b["c"] for b in bars[s] if b["start"] + datetime.timedelta(minutes=india.BAR_MIN) <= cutoff]
            r5 = tech.rsi(closes)[-1] if len(closes) > 15 else None
            intraday.append(tech.enrich_intraday(row, ind.get(s), _pct_today(bars[s], day, cutoff), nifty_pct, r5,
                                                 tech.next_event(events, s, day, 1), reg))
        ranked = india.rank_setups(intraday, int(rules.get("max_trades_per_day", 3)), tech.INTRADAY_MIN_SCORE)
        # short term
        swing = []
        nifty_3m = (ind.get("NIFTY") or {}).get("ret_3m")
        for s in syms:
            if s not in ind:
                continue
            sb = india.session_bars(bars.get(s) or [], day, cutoff)
            swing.append(tech.swing_setup(s, ind[s], nifty_3m, reg, fund.get(s), industry.get(s) in tech.FINANCIAL,
                                          tech.next_event(events, s, day, 15), capital, swing_loss, rules,
                                          sb[-1]["c"] if sb else None))
        swing = tech.rank_swing(swing, int(rules["swing_max_positions"]), int(rules["swing_min_score"]))
        late = live and now.time() >= india.parse_hhmm(rules["flat_by"])
        d.update({"now_ist": now.strftime("%Y-%m-%d %H:%M:%S"), "session": day.isoformat(),
                  "cutoff": cutoff.strftime("%H:%M"), "live_session": live, "rules": rules, "capital": capital,
                  "max_loss": max_loss, "swing_max_loss": swing_loss, "trends": trends, "options": options,
                  "setups": ranked, "swing": swing, "market": regime, "nifty_pct": nifty_pct,
                  "flat_by_passed": late})
        return d

    def chart(self, sym, tf="5m"):
        day, cutoff = india.session_day(self.now_ist())
        with self.lock:
            bars = list((self.data["daily"] if tf == "1d" else self.data["bars"]).get(sym) or [])
        if not bars:
            return None
        if tf == "1d":
            c = [b["c"] for b in bars]
            lines = {"sma20": tech.sma(c, 20), "sma50": tech.sma(c, 50), "sma200": tech.sma(c, 200)}
            out = [dict(_candle(b), **{k: (round(v[i], 2) if v[i] is not None else None) for k, v in lines.items()})
                   for i, b in enumerate(bars)]
            return {"symbol": sym, "tf": "1d", "bars": out}
        # the candle still forming is drawn too; the 1-second ticks keep moving it
        sb = india.session_bars(bars, day, cutoff + datetime.timedelta(minutes=india.BAR_MIN))
        upto = [b for b in bars if b["start"].date() <= day]
        ema_by = dict((b["start"], v) for b, v in zip(upto, india.ema([b["c"] for b in upto])))
        cum_pv = cum_v = 0.0
        out = []
        for b in sb:
            cum_pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            cum_v += b["v"]
            out.append(dict(_candle(b), vwap=round(cum_pv / cum_v, 2) if cum_v else None,
                            ema=round(ema_by.get(b["start"], b["c"]), 2)))
        return {"symbol": sym, "tf": "5m", "day": day.isoformat(), "bars": out}


def _candle(b):
    # Lightweight Charts wants UTC seconds; the IST wall clock as if it were UTC makes the axis read IST
    return {"time": int((b["start"] - datetime.datetime(1970, 1, 1)).total_seconds()),
            "open": b["o"] if b["o"] is not None else b["c"], "high": b["h"], "low": b["l"], "close": b["c"],
            "volume": b["v"]}


def _pct_today(bars, day, cutoff):
    """Percent change from the previous session's last 5-minute close to the latest one."""
    if not bars:
        return None
    before = [b for b in bars if b["start"].date() < day]
    sb = india.session_bars(bars, day, cutoff)
    if not before or not sb:
        return None
    return round(100.0 * (sb[-1]["c"] / before[-1]["c"] - 1), 2)


PAGE = os.path.join(HERE, "dashboard.html")


def make_handler(hub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)

            def num(k):
                try:
                    return float(q[k][0]) if k in q and float(q[k][0]) > 0 else None
                except ValueError:
                    return None
            try:
                if u.path == "/":
                    with open(PAGE, "rb") as fh:
                        return self.send(200, fh.read(), "text/html")
                if u.path == "/api/state":
                    return self.send(200, json.dumps(hub.state(num("capital"), num("max_loss"))))
                if u.path == "/api/tick":
                    with hub.lock:
                        return self.send(200, json.dumps(hub.data["tick"]))
                if u.path == "/api/chart":
                    c = hub.chart((q.get("symbol") or [""])[0].upper(), (q.get("tf") or ["5m"])[0])
                    return self.send(200 if c else 404, json.dumps(c or {"error": "no bars yet for that symbol"}))
                return self.send(404, json.dumps({"error": "not found"}))
            except Exception as e:
                traceback.print_exc()
                return self.send(500, json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live NSE dashboard for trader-india.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default="trader/india/config.json")
    ap.add_argument("--offline", metavar="DIR", help="read saved files once, no network")
    ap.add_argument("--now", help="pretend it is this time (no offset = IST)")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    hub = Hub(a.offline, a.now, a.config)
    print("Reading NSE and Yahoo (the first NIFTY 50 scan takes about 35 seconds, the short-term screen "
          "about a minute more)...")
    for fn, every in ((hub.tick, TICK), (hub.fast, FAST), (hub.slow, SLOW), (hub.daily, DAILY)):
        threading.Thread(target=hub.loop, args=(fn, every), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(hub))
    url = "http://127.0.0.1:%d/" % a.port
    print("Dashboard: %s  (Ctrl+C to stop)" % url)
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
