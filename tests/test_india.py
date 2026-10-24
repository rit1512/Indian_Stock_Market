import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

india = load("trader-india", "india")

IDIR = os.path.join(FIX, "trader-india")
DT = datetime.datetime
D = datetime.date
EIGHT = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ICICIBANK", "SBIN", "M&M", "TATASTEEL"]

# NSE capital-market trading holidays, https://www.nseindia.com/api/holiday-master?type=trading (read 2026-10-24),
# weekdays only
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
            "2026-11-24", "2026-12-25"]


def ist(text):
    return india.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = india.main(argv)
    return code, out.getvalue(), err.getvalue()


def bars(sym):
    with open(os.path.join(IDIR, "yahoo-%s.json" % india.stem(india.yahoo_symbol(sym)))) as fh:
        return india.parse_bars(fh.read(), india.yahoo_symbol(sym))


class Clock(unittest.TestCase):
    def test_holidays_match_nse(self):
        self.assertEqual(sorted(india.NSE_HOLIDAYS), NSE_2026)
        for d in india.NSE_HOLIDAYS:
            self.assertLess(D.fromisoformat(d).weekday(), 5, d)

    def test_ist_is_utc_plus_five_thirty(self):
        self.assertEqual(ist("2026-10-23 09:15"), DT(2026, 10, 23, 3, 45))
        self.assertEqual(ist("2026-10-23T03:45:00Z"), DT(2026, 10, 23, 3, 45))
        self.assertEqual(india.to_ist(DT(2026, 10, 23, 10, 0)), DT(2026, 10, 23, 15, 30))
        with self.assertRaises(india.IndiaError):
            ist("monday")

    def test_phases_of_a_trading_day(self):
        phase = lambda t: india.market_status(ist("2026-10-23 " + t))["phase"]
        self.assertEqual(phase("08:59"), "closed")
        self.assertEqual(phase("09:00"), "pre-open")
        self.assertEqual(phase("09:14"), "pre-open")
        self.assertEqual(phase("09:15"), "open")
        self.assertEqual(phase("15:29"), "open")
        self.assertEqual(phase("15:30"), "post-close")
        self.assertEqual(phase("16:00"), "closed")
        self.assertTrue(india.market_status(ist("2026-10-23 12:00"))["open"])
        self.assertEqual(india.market_status(ist("2026-10-23 09:05"))["next_open"], "2026-10-23 09:15")

    def test_holidays_weekends_and_muhurat(self):
        dussehra = india.market_status(ist("2026-10-20 11:00"))
        self.assertFalse(dussehra["open"])
        self.assertIn("Dussehra", dussehra["reason"])
        self.assertEqual(dussehra["next_open"], "2026-10-21 09:15")
        sat = india.market_status(ist("2026-10-24 15:00"))
        self.assertEqual((sat["reason"], sat["next_open"]), ("weekend", "2026-10-26 09:15"))
        diwali = india.market_status(ist("2026-11-08 18:00"))
        self.assertFalse(diwali["open"])
        self.assertTrue(diwali["muhurat"])
        # Nov 10 is Balipratipada, so after the Muhurat Sunday the market opens on the 9th
        self.assertEqual(diwali["next_open"], "2026-11-09 09:15")
        self.assertEqual(india.market_status(ist("2026-11-09 16:30"))["next_open"], "2026-11-11 09:15")
        self.assertFalse(india.market_status(ist("2027-01-04 10:00"))["calendar_known"])

    def test_clock_live_reads_nse(self):
        code, out, _ = run(["clock", "--now", "2026-10-24 15:00", "--live", "--offline", IDIR])
        self.assertEqual(code, 1)
        self.assertIn("NSE closed, weekend", out)
        self.assertIn("NSE says: Market is Closed (23-Oct-2026 15:30), NIFTY 50 22600 (-0.39%)", out)


class Symbols(unittest.TestCase):
    def test_yahoo_symbols(self):
        y = india.yahoo_symbol
        self.assertEqual(y("reliance"), "RELIANCE.NS")
        self.assertEqual(y("RELIANCE", bse=True), "RELIANCE.BO")
        self.assertEqual(y("nifty"), "^NSEI")
        self.assertEqual(y("BANKNIFTY"), "^NSEBANK")
        self.assertEqual(y("SENSEX"), "^BSESN")
        self.assertEqual(y("FINNIFTY"), "NIFTY_FIN_SERVICE.NS")
        self.assertEqual(y("TCS.NS"), "TCS.NS")
        self.assertEqual(y("^INDIAVIX"), "^INDIAVIX")
        self.assertEqual(india.display("^NSEI"), "NIFTY")
        self.assertEqual(india.display("M&M.NS"), "M&M")

    def test_ampersand_is_escaped_in_the_url(self):
        url = india.yahoo_url("M&M.NS", "1d", "5m")
        self.assertIn("/v8/finance/chart/M%26M.NS?range=1d&interval=5m", url)
        self.assertEqual(india.stem("M&M.NS"), "M_M.NS")
        self.assertEqual(india.stem("^NSEI"), "_NSEI")


class Quotes(unittest.TestCase):
    def test_quotes_from_yahoo(self):
        quotes, errors = india.get_quotes(["RELIANCE", "M&M", "NIFTY", "SENSEX", "NOPE"], offline=IDIR)
        by = {q["symbol"]: q for q in quotes}
        self.assertEqual(by["RELIANCE"]["price"], 1190.0)
        self.assertEqual(by["RELIANCE"]["prev_close"], 1184.5)
        self.assertEqual(by["RELIANCE"]["pct"], 0.46)
        self.assertEqual(by["RELIANCE"]["currency"], "INR")
        self.assertEqual(by["RELIANCE"]["time_ist"], "2026-10-23 15:28")
        self.assertEqual(by["M&M"]["price"], 2861.2)
        self.assertEqual(by["NIFTY"]["price"], 22600.0)
        self.assertEqual(by["SENSEX"]["exchange"], "BSE")
        self.assertEqual(len(errors), 1)
        self.assertIn("NOPE.NS", errors[0])

    def test_quote_write(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "india", "prices.json")
            code, out, _ = run(["quote", "RELIANCE", "TCS", "--offline", IDIR, "--write", path,
                                "--now", "2026-10-23 15:35"])
            self.assertEqual(code, 0)
            self.assertIn("RELIANCE    1190.00 (+0.46% vs prev close 1184.50)", out)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["currency"], "INR")
            self.assertEqual(doc["prices"], {"RELIANCE": 1190.0, "TCS": 2530.0})
            self.assertEqual(doc["as_of"], "2026-10-23T10:05:00Z")
        finally:
            shutil.rmtree(tmp)


class Indices(unittest.TestCase):
    def test_nse_indices_and_breadth(self):
        with open(os.path.join(IDIR, "nse-allIndices.json")) as fh:
            out = india.indices_nse(json.load(fh))
        names = [r["index"] for r in out["indices"]]
        self.assertEqual(names[:3], ["NIFTY 50", "NIFTY BANK", "NIFTY FINANCIAL SERVICES"])
        self.assertEqual(names[-1], "INDIA VIX")
        nifty = out["indices"][0]
        self.assertEqual((nifty["last"], nifty["pct"], nifty["prev_close"]), (22600, -0.39, 22687.65))
        self.assertEqual((out["advances"], out["declines"]), (1084, 8476))

    def test_falls_back_to_yahoo_when_nse_is_missing(self):
        tmp = tempfile.mkdtemp()
        try:
            for f in os.listdir(IDIR):
                if f.startswith("yahoo-_"):
                    shutil.copy(os.path.join(IDIR, f), tmp)
            code, out, _ = run(["indices", "--offline", tmp])
            self.assertEqual(code, 0)
            self.assertIn("so these are from Yahoo", out)
            self.assertIn("SENSEX", out)
            self.assertIn("73123.83  -0.17%", out)
        finally:
            shutil.rmtree(tmp)

    def test_movers(self):
        code, out, _ = run(["movers", "--top", "3", "--offline", IDIR, "--json"])
        m = json.loads(out)
        self.assertEqual([r["symbol"] for r in m["gainers"]], ["ETERNAL", "APOLLOHOSP", "ONGC"])
        self.assertEqual(m["gainers"][0]["pct"], 4.25)
        self.assertEqual([r["symbol"] for r in m["losers"]], ["SHRIRAMFIN", "ADANIENT", "JSWSTEEL"])
        self.assertEqual(len(m["losers"]), 3)


class Orb(unittest.TestCase):
    def test_reliance_breaks_down_at_10_15(self):
        day = D(2026, 10, 23)
        r = india.orb(bars("RELIANCE"), day, DT(2026, 10, 23, 10, 15), 15)
        self.assertEqual((r["or_high"], r["or_low"]), (1201.6, 1183.0))
        self.assertEqual((r["signal"], r["price"], r["at"]), ("breakdown", 1178.3, "10:15"))
        # at 10:14 the 10:10 bar is still forming
        self.assertEqual(india.orb(bars("RELIANCE"), day, DT(2026, 10, 23, 10, 14), 15)["signal"], "inside")

    def test_hdfcbank_breaks_out_later(self):
        day = D(2026, 10, 23)
        r = india.orb(bars("HDFCBANK"), day, DT(2026, 10, 23, 15, 30), 15)
        self.assertEqual((r["signal"], r["at"]), ("breakdown", "10:05"))  # the first close outside wins

    def test_incomplete_range_is_none(self):
        self.assertIsNone(india.orb(bars("TCS"), D(2026, 10, 23), DT(2026, 10, 23, 9, 25), 15))
        self.assertIsNone(india.orb(bars("TCS"), D(2026, 10, 22), DT(2026, 10, 22, 10, 0), 15))

    def test_scan_cli(self):
        code, out, _ = run(["orb"] + EIGHT + ["--date", "2026-10-23", "--at", "10:15", "--offline", IDIR])
        self.assertEqual(code, 0)
        self.assertIn("Breakouts (first 5-minute close above the range): 0", out)
        self.assertIn("Breakdowns (first 5-minute close below the range): 8", out)
        self.assertIn("RELIANCE    1178.30 at 10:15  range 1183 to 1201.60  (-0.40%)", out)
        self.assertIn("Shorting is off", out)

    def test_scan_refuses_early_or_on_a_holiday(self):
        code, _, err = run(["orb", "TCS", "--date", "2026-10-23", "--at", "09:25", "--offline", IDIR])
        self.assertEqual(code, 1)
        self.assertIn("not complete until 09:30 IST", err)
        code, _, err = run(["orb", "TCS", "--date", "2026-10-20", "--at", "10:00", "--offline", IDIR])
        self.assertIn("Dussehra", err)

    def test_universe_is_the_nifty_50(self):
        with open(os.path.join(IDIR, "ind_nifty50list.csv")) as fh:
            syms = india.parse_nifty50(fh.read())
        self.assertEqual(len(syms), 50)
        self.assertIn("RELIANCE", syms)
        self.assertIn("M&M", syms)


class Charges(unittest.TestCase):
    def test_round_trip_on_one_lakh(self):
        c = india.charges(100000, 100500)
        self.assertEqual(c["brokerage"], 40.0)          # 0.03 % is over Rs 20, so Rs 20 a side
        self.assertEqual(c["stt"], 25.12)               # 0.025 % of the sell
        self.assertEqual(c["exchange_txn"], 6.16)       # 0.00307 % of both
        self.assertEqual(c["sebi"], 0.2)
        self.assertEqual(c["stamp_duty"], 3.0)          # 0.003 % of the buy
        self.assertEqual(c["gst"], 8.34)                # 18 % of brokerage + txn + SEBI
        self.assertEqual(c["total"], 82.82)

    def test_small_orders_pay_point_zero_three_percent(self):
        self.assertEqual(india.charges(10000, 10000)["brokerage"], 6.0)
        self.assertGreater(india.charges(10000, 10000, "bse")["exchange_txn"],
                           india.charges(10000, 10000, "nse")["exchange_txn"])

    def test_config_overrides_rates(self):
        self.assertEqual(india.charges(100000, 100000, rates={"brokerage_max": 0.0})["brokerage"], 0.0)

    def test_rupee_grouping(self):
        self.assertEqual(india.rs(1234567.5), "Rs 12,34,567.50")
        self.assertEqual(india.rs(500), "Rs 500.00")
        self.assertEqual(india.rs(100000), "Rs 1,00,000.00")
        self.assertEqual(india.rs(-52.9), "-Rs 52.90")


class Size(unittest.TestCase):
    def test_max_loss_includes_charges(self):
        p = india.size(1190, 1183, 1204, max_loss=500)
        # 71 shares by risk alone; charges trim it: 61 x 7 = 427 + 72.83 = 499.83, and 62 would be over
        self.assertEqual(p["qty"], 61)
        self.assertEqual(p["charges_at_stop"]["total"], 72.83)
        self.assertEqual(p["loss_at_stop"], 499.83)
        self.assertGreater(p["reward_risk"], 1.5)

    def test_capital_caps_the_shares(self):
        p = india.size(1190, 1183, 1204, max_loss=500, capital=50000)
        self.assertEqual((p["qty"], p["value"]), (42, 49980.0))

    def test_wrong_side_and_too_wide(self):
        with self.assertRaises(india.IndiaError):
            india.size(1190, 1200)
        with self.assertRaises(india.IndiaError):
            india.size(1190, 1183, target=1180)
        with self.assertRaises(india.IndiaError):
            india.size(1190, 600, max_loss=500)

    def test_short_and_leverage_need_the_config(self):
        code, _, err = run(["size", "--entry", "1190", "--stop", "1200", "--short", "--config", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("shorting is off", err)
        code, _, err = run(["size", "--entry", "1190", "--stop", "1183", "--leverage", "5",
                            "--config", "/nonexistent"])
        self.assertIn("leverage is off", err)
        tmp = tempfile.mkdtemp()
        try:
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w") as fh:
                json.dump({"allow_short": True}, fh)
            p = india.size(1190, 1200, 1170, max_loss=500, short=True)
            self.assertEqual(p["side"], "short")
            code, out, _ = run(["size", "--entry", "1190", "--stop", "1200", "--short", "--config", cfg])
            self.assertEqual(code, 0)
            self.assertIn("Sell short", out)
        finally:
            shutil.rmtree(tmp)

    def test_size_cli_reads_the_config(self):
        code, out, _ = run(["size", "--entry", "1190", "--stop", "1183", "--target", "1204", "--config",
                            os.path.join(os.path.dirname(IDIR), "..", "..", "trader-india", "config.example.json")])
        self.assertEqual(code, 0)
        self.assertIn("Buy 42 at 1190 (Rs 49,980.00, NSE)", out)
        self.assertIn("max Rs 500.00", out)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._open, self._sleep = india._urlopen, india._sleep
        india._sleep = lambda s: None

    def tearDown(self):
        india._urlopen, india._sleep = self._open, self._sleep

    def test_nse_reloads_cookies_once_on_403(self):
        answers = [403, 200, 200]

        class R(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=20):
            self.calls.append((req.full_url, req.get_header("User-agent")))
            code = answers.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "Forbidden", {}, io.BytesIO(b""))
            return R(b'{"marketState": []}')
        india._urlopen = fake
        self.assertEqual(india.fetch_nse("marketStatus"), {"marketState": []})
        self.assertEqual([u for u, _ in self.calls], ["https://www.nseindia.com/api/marketStatus",
                                                      "https://www.nseindia.com/",
                                                      "https://www.nseindia.com/api/marketStatus"])
        self.assertTrue(all("Chrome" in ua for _, ua in self.calls))

    def test_yahoo_falls_back_to_query1(self):
        class R(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=20):
            self.calls.append(req.full_url)
            if "query2" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
            return R(b"{}")
        india._urlopen = fake
        self.assertEqual(india.fetch_yahoo("TCS.NS"), "{}")
        self.assertEqual(["query2" in u for u in self.calls], [True, False])


if __name__ == "__main__":
    unittest.main()
