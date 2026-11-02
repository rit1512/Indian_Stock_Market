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


LIVE = os.path.join(IDIR, "live-2026-10-27")   # NSE and Yahoo at 12:10 IST on Tuesday 2026-10-27
NOON = DT(2026, 10, 27, 12, 10)


def chain(sym, expiry):
    return india.option_chain(india.nse_doc("option-chain-v3?type=Indices&symbol=%s&expiry=%s" % (sym, expiry), LIVE))


def live_bars(sym):
    y = india.yahoo_symbol(sym)
    return india.parse_bars(india.yahoo_text(y, LIVE), y)


class Options(unittest.TestCase):
    def test_offline_names(self):
        self.assertEqual(india.offline_name("allIndices"), "allIndices")
        self.assertEqual(india.offline_name("live-analysis-variations?index=loosers"), "loosers")
        self.assertEqual(india.offline_name("option-chain-v3?type=Indices&symbol=NIFTY&expiry=03-Nov-2026"),
                         "option-chain-v3-NIFTY-03-Nov-2026")
        self.assertEqual(india.offline_name("option-chain-contract-info?symbol=BANKNIFTY"),
                         "option-chain-contract-info-BANKNIFTY")

    def test_expiry_day_is_skipped(self):
        info = india.nse_doc("option-chain-contract-info?symbol=NIFTY", LIVE)
        self.assertEqual(india.pick_expiry(info, D(2026, 10, 27)), ("03-Nov-2026", "27-Oct-2026"))
        self.assertEqual(india.pick_expiry(info, D(2026, 10, 26)), ("27-Oct-2026", None))
        bn = india.nse_doc("option-chain-contract-info?symbol=BANKNIFTY", LIVE)
        self.assertEqual(india.pick_expiry(bn, D(2026, 10, 27))[0], "23-Nov-2026")  # BANKNIFTY is monthly

    def test_lot_sizes_from_nse(self):
        with open(os.path.join(LIVE, "fo_mktlots.csv")) as fh:
            lots = india.parse_lots(fh.read())
        self.assertEqual((lots["NIFTY"], lots["BANKNIFTY"]), (65, 30))
        self.assertEqual(india.LOTS, {"NIFTY": 65, "BANKNIFTY": 30})

    def test_chain_stats(self):
        c = chain("NIFTY", "03-Nov-2026")
        self.assertEqual((c["spot"], c["expiry"], c["time"]), (22760.1, "03-Nov-2026", "27-Oct-2026 12:10:22"))
        st = india.option_stats(c)
        self.assertEqual((st["atm"], st["ce_ltp"], st["pe_ltp"]), (22750.0, 228, 183.5))
        self.assertEqual((st["pcr"], st["max_pain"], st["call_wall"], st["put_wall"]), (0.74, 22750.0, 23000.0, 22500.0))
        bn = india.option_stats(chain("BANKNIFTY", "23-Nov-2026"))
        self.assertEqual((bn["atm"], bn["pcr"], bn["call_wall"], bn["put_wall"]), (54300.0, 1.12, 55000.0, 54000.0))

    def test_walls_ignore_far_strikes(self):
        c = {"spot": 100.0, "rows": [
            {"strike": 50.0, "ce": {"oi": 0, "ltp": 0, "iv": 0}, "pe": {"oi": 9999, "ltp": 0, "iv": 0}},
            {"strike": 98.0, "ce": {"oi": 5, "ltp": 3, "iv": 0}, "pe": {"oi": 40, "ltp": 1, "iv": 0}},
            {"strike": 100.0, "ce": {"oi": 10, "ltp": 2, "iv": 0}, "pe": {"oi": 10, "ltp": 2, "iv": 0}},
            {"strike": 103.0, "ce": {"oi": 50, "ltp": 1, "iv": 0}, "pe": {"oi": 1, "ltp": 4, "iv": 0}}]}
        st = india.option_stats(c)
        self.assertEqual((st["put_wall"], st["call_wall"], st["atm"]), (98.0, 103.0, 100.0))

    def test_index_trend_at_noon(self):
        t = india.index_trend(live_bars("NIFTY"), D(2026, 10, 27), NOON)
        self.assertEqual((t["or_low"], t["or_high"], t["last"], t["ema"]), (22653.5, 22745.95, 22754.6, 22755.64))
        self.assertEqual(t["bias"], "neutral")   # over the range, a hair under the EMA
        bn = india.index_trend(live_bars("BANKNIFTY"), D(2026, 10, 27), NOON)
        self.assertEqual((bn["bias"], bn["why"]), ("neutral", "inside its opening range"))

    def test_no_option_trade_when_neutral(self):
        t = india.index_trend(live_bars("NIFTY"), D(2026, 10, 27), NOON)
        u = india.option_setup("NIFTY", t, india.option_stats(chain("NIFTY", "03-Nov-2026")), 65, 500)
        self.assertEqual((u["action"], u["check"]), ("NO TRADE", "WAIT"))

    def test_call_when_bullish_put_when_bearish(self):
        st = india.option_stats(chain("NIFTY", "03-Nov-2026"))
        bull = {"or_high": 22745.95, "or_low": 22653.5, "last": 22800.0, "bias": "bullish", "why": "above"}
        u = india.option_setup("NIFTY", bull, st, 65, 5000, {"allow_options": True, "no_entries_after": "14:45"},
                               datetime.time(11, 0))
        self.assertEqual(u["action"], "BUY NIFTY 22750 CE")
        self.assertEqual((u["index_stop"], u["index_target"]), (22699.72, 23000.56))   # range middle, 2x
        self.assertEqual((u["premium_stop"], u["premium_target"]), (177.86, 328.28))   # delta 0.5
        self.assertEqual((u["risk_per_lot"], u["lots"], u["check"]), (3259.1, 1, "PASS"))   # (228 - 177.86) x 65
        self.assertIn("23000", u["wall_note"])
        bear = dict(bull, last=22600.0, bias="bearish")
        p = india.option_setup("NIFTY", bear, st, 65, 500)
        self.assertEqual((p["action"], p["check"]), ("BUY NIFTY 22750 PE", "BLOCK"))
        self.assertIn("options are off", p["reasons"][0])
        self.assertIn("over your Rs 500 max loss", p["reasons"][1])
        late = india.option_setup("NIFTY", bull, st, 65, 5000, {"allow_options": True, "no_entries_after": "14:45"},
                                  datetime.time(14, 50))
        self.assertEqual(late["reasons"], ["after 14:45, no new entries"])


class Setups(unittest.TestCase):
    def setUp(self):
        self.day = D(2026, 10, 27)
        self.rules = {"no_entries_after": "14:45"}

    def setup_for(self, sym, bias="neutral"):
        return india.stock_setup(sym, live_bars(sym), self.day, NOON, 15, 50000, 500, self.rules, bias,
                                 datetime.time(12, 10))

    def test_bel_still_holding(self):
        s = self.setup_for("BEL")
        self.assertEqual((s["status"], s["entry"], s["stop"], s["target"]), ("holding", 370.55, 366.05, 379.55))
        self.assertEqual((s["qty"], s["risk"], s["vwap"], s["vol_x"]), (102, 498.88, 370.97, 1.43))
        self.assertEqual((s["score"], s["check"]), (2, "PASS"))   # holding, under VWAP, normal volume
        self.assertEqual(self.setup_for("BEL", "bullish")["score"], 3)

    def test_faded_stopped_and_shorts(self):
        t = self.setup_for("TATACONSUM")
        self.assertEqual((t["status"], t["entry"]), ("faded", 989.55))   # buy-stop at the breakout close
        e = self.setup_for("ETERNAL")
        self.assertEqual((e["status"], e["check"]), ("stopped", "BLOCK"))
        r = self.setup_for("RELIANCE")
        self.assertEqual((r["status"], r["check"], r["reasons"]), ("breakdown", "SKIP", ["long only"]))
        self.assertEqual(self.setup_for("HDFCBANK")["status"], "inside")

    def test_ranking_needs_a_score_of_three_to_pick(self):
        rows = india.rank_setups([self.setup_for(s) for s in ("BEL", "ETERNAL", "RELIANCE", "TATACONSUM")])
        self.assertEqual([r["symbol"] for r in rows], ["BEL", "TATACONSUM", "ETERNAL", "RELIANCE"])
        self.assertFalse(any(r["pick"] for r in rows))
        rows = india.rank_setups([self.setup_for(s, "bullish") for s in ("BEL", "TATACONSUM")], max_trades=1)
        self.assertEqual([r["pick"] for r in rows], [True, False])

    def test_session_day(self):
        self.assertEqual(india.session_day(DT(2026, 10, 27, 12, 10)), (self.day, NOON))
        self.assertEqual(india.session_day(DT(2026, 10, 27, 9, 20)), (D(2026, 10, 26), DT(2026, 10, 26, 15, 30)))
        self.assertEqual(india.session_day(DT(2026, 10, 27, 18, 0)), (self.day, DT(2026, 10, 27, 15, 30)))
        self.assertEqual(india.session_day(DT(2026, 10, 21, 9, 0)), (D(2026, 10, 19), DT(2026, 10, 19, 15, 30)))

    def test_vwap_and_ema(self):
        self.assertIsNone(india.vwap([{"h": 2, "l": 1, "c": 1.5, "v": 0}]))
        self.assertEqual(india.vwap([{"h": 3, "l": 1, "c": 2, "v": 1}, {"h": 6, "l": 4, "c": 5, "v": 3}]), 4.25)
        self.assertEqual(india.ema([1, 1, 1]), [1, 1, 1])
        self.assertAlmostEqual(india.ema([0, 21], 20)[-1], 2.0)


tech = load("trader-india", "technicals")


def daily(sym):
    y = india.yahoo_symbol(sym)
    return india.parse_bars(india._offline(LIVE, "daily-%s.json" % india.stem(y)), y)


def fundamentals(sym):
    return tech.parse_fundamentals(india._offline(LIVE, "fundamentals-%s.NS.json" % sym))


class Indicators(unittest.TestCase):
    def test_small_cases(self):
        self.assertEqual(tech.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(tech.rsi(list(range(1, 17)))[-1], 100.0)            # only gains
        self.assertAlmostEqual(tech.rsi([10, 11] * 8)[-1], 53.57, places=2)  # Wilder smoothing
        self.assertEqual(tech.atr([{"h": 10, "l": 8, "c": 9}] * 15)[-1], 2.0)
        self.assertEqual(tech.macd([5.0] * 40)[2][-1], 0.0)

    def test_nifty_daily(self):
        n = tech.daily_indicators(daily("NIFTY"))
        self.assertEqual((n["date"], n["close"], n["trend"]), ("2026-10-27", 22766.35, "downtrend"))
        self.assertEqual((n["sma20"], n["sma50"], n["sma200"]), (22679.25, 23337.48, 24091.58))
        self.assertEqual((n["rsi"], n["atr"], n["ret_3m"]), (46.39, 290.09, -5.12))

    def test_eternal_daily(self):
        e = tech.daily_indicators(daily("ETERNAL"))
        self.assertEqual((e["trend"], e["rsi"], e["atr"], e["atr_pct"]), ("uptrend", 53.19, 10.22, 3.14))
        self.assertEqual((e["macd_hist"], e["macd_hist_prev"]), (0.03, -0.6))   # just turned up
        with self.assertRaises(tech.IndiaError):   # technicals imports its own copy of india
            tech.daily_indicators(daily("ETERNAL")[:30])


class Market(unittest.TestCase):
    def test_bearish_regime_on_the_27th(self):
        n = tech.daily_indicators(daily("NIFTY"))
        flows = tech.parse_flows(india.nse_doc("fiidiiTradeReact", LIVE))
        self.assertEqual(flows, [{"category": "DII", "date": "26-Oct-2026", "net": 2609.57},
                                 {"category": "FII/FPI", "date": "26-Oct-2026", "net": -1462.49}])
        m = tech.market_regime(n, 16.7, 7133, 2420, 0.74, flows)
        self.assertEqual((m["regime"], m["risk"], m["breadth"]), ("bearish", "normal", 75))
        self.assertIn("NIFTY downtrend, below its 50-day (23337) and below its 200-day (24092)", m["notes"])
        self.assertIn("FII/FPI net Rs -1462.49 cr on 26-Oct-2026", m["notes"])
        self.assertEqual(tech.market_regime(n, 21)["risk"], "high")
        up = dict(n, close=24500.0, sma50=24200.0, sma200=24000.0, trend="uptrend")
        self.assertEqual(tech.market_regime(up, 12)["regime"], "bullish")

    def test_results_calendar(self):
        path = tech.events_path(D(2026, 10, 28), 7)
        self.assertEqual(path, "event-calendar?index=equities&from_date=28-10-2026&to_date=04-11-2026")
        self.assertEqual(india.offline_name(path), "event-calendar")   # not "equities"
        ev = tech.parse_events(india.nse_doc(path, LIVE))
        self.assertEqual(len(ev), 23)
        today = D(2026, 10, 27)
        self.assertEqual(tech.next_event(ev, "BEL", today, 15),
                         {"date": "2026-10-30", "purpose": "Financial Results", "in_days": 3})
        self.assertIsNone(tech.next_event(ev, "BEL", today, 2))
        self.assertEqual(tech.next_event(ev, "ADANIPORTS", today, 15)["in_days"], 1)


class Fundamentals(unittest.TestCase):
    def test_parse(self):
        f = fundamentals("BEL")
        self.assertEqual((f["pe"], f["pb"], f["roe"], f["de"]), (43.1, 11.03, 25.3, 0.0))
        self.assertEqual((f["rev_growth"], f["profit_growth"], f["fcf_cr"], f["year"]), (15.9, 13.9, 556, "2026-03-31"))

    def test_checks(self):
        self.assertEqual(tech.fundamental_checks(fundamentals("BEL"))["grade"], "strong")
        e = tech.fundamental_checks(fundamentals("ETERNAL"))
        self.assertEqual((e["passed"], e["of"], e["grade"]), (3, 6, "ok"))
        self.assertIn("rich valuation, P/E 610", e["notes"])
        h = tech.fundamental_checks(fundamentals("HDFCBANK"), financial=True)
        self.assertEqual((h["passed"], h["of"]), (3, 4))   # no debt/equity or FCF check for a bank
        self.assertEqual(tech.fundamental_checks(None)["grade"], "unknown")


class ShortTerm(unittest.TestCase):
    def setUp(self):
        self.nifty = tech.daily_indicators(daily("NIFTY"))
        self.e = tech.daily_indicators(daily("ETERNAL"))

    def run_eternal(self, regime, event=None):
        return tech.swing_setup("ETERNAL", self.e, self.nifty["ret_3m"], regime, fundamentals("ETERNAL"), False,
                                event, 50000, 500)

    def test_pullback_blocked_in_a_bearish_market(self):
        r = self.run_eternal("bearish")
        self.assertEqual((r["setup"], r["check"], r["reasons"]), ("pullback", "BLOCK", ["the market regime is bearish"]))
        self.assertEqual((r["tech"], r["fund"], r["market"], r["score"]), (6, 1, 0, 7))
        self.assertEqual((r["entry"], r["stop"], r["target"], r["qty"]), (325.9, 305.46, 366.78, 22))   # 2 ATR stop
        self.assertEqual((r["risk"], r["charges"]), (480.51, 30.83))   # delivery charges, DP included
        self.assertIn("52-week high 344.40 sits below the target", r["flags"])

    def test_pass_when_bullish_and_blocked_before_results(self):
        r = self.run_eternal("bullish")
        self.assertEqual((r["check"], r["score"]), ("PASS", 8))
        r = self.run_eternal("bullish", {"date": "2026-10-30", "purpose": "Financial Results", "in_days": 3})
        self.assertEqual(r["check"], "BLOCK")
        self.assertIn("gap risk", r["reasons"][0])
        r = self.run_eternal("bullish", {"date": "2026-11-10", "purpose": "Financial Results", "in_days": 14})
        self.assertEqual(r["check"], "PASS")

    def test_downtrend_is_skipped(self):
        b = tech.swing_setup("BEL", tech.daily_indicators(daily("BEL")), self.nifty["ret_3m"], "bullish",
                             fundamentals("BEL"), False, None, 50000, 500)
        self.assertEqual((b["setup"], b["check"], b["reasons"]), ("none", "SKIP", ["not in an uptrend (downtrend)"]))

    def test_rank(self):
        rows = tech.rank_swing([self.run_eternal("bullish"), self.run_eternal("bearish")], min_score=8)
        self.assertEqual([(r["check"], r["pick"]) for r in rows], [("PASS", True), ("BLOCK", False)])

    def test_delivery_charges(self):
        c = india.charges(100000, 100500, rates=india.DELIVERY)
        self.assertEqual((c["brokerage"], c["stt"], c["stamp_duty"], c["dp"], c["total"]), (0.0, 200.5, 15.0, 15.34, 238.34))


class IntradayExtras(unittest.TestCase):
    def test_enrich(self):
        row = {"symbol": "X", "check": "PASS", "score": 4, "reasons": []}
        up = {"trend": "uptrend"}
        r = tech.enrich_intraday(row, up, 1.5, 0.2, 85, None, "bearish")
        self.assertEqual((r["score"], r["rs_today"], r["check"]), (6, 1.3, "PASS"))
        self.assertEqual(r["flags"], ["5-min RSI 85: chasing", "market regime bearish: a long fights the bigger trend"])
        r = tech.enrich_intraday(row, up, 1.5, 0.2, 50, {"date": "2026-10-27", "purpose": "Financial Results", "in_days": 0})
        self.assertEqual(r["check"], "BLOCK")
        self.assertEqual(row["score"], 4)   # the input is not changed
        self.assertEqual(tech.enrich_intraday({"check": "SKIP", "score": 0}, up)["score"], 0)


class Ticks(unittest.TestCase):
    def test_parse_spark(self):
        t = india.parse_ticks(india.tick_text(["^NSEI", "^NSEBANK"], LIVE))
        self.assertEqual(t["^NSEI"]["price"], 22769.7)
        self.assertEqual(t["^NSEI"]["time_ist"], "12:32:19")
        self.assertEqual(t["^NSEBANK"]["price"], 54291.35)
        self.assertIn("&_=", india.spark_url(["^NSEI"]))   # the cache-buster

    def test_hub_never_steps_back(self):
        dash = load("trader-india", "dashboard")
        hub = dash.Hub(LIVE, "2026-10-27 12:33", "/nonexistent")
        hub.data["tick"] = {"interval": 1, "NIFTY": {"price": 22790.0, "ts": 1793084999, "time_ist": "12:36:39"}}
        self.assertEqual(hub.tick(), 1)
        self.assertEqual(hub.data["tick"]["NIFTY"]["price"], 22790.0)    # the file's tick is older
        self.assertEqual(hub.data["tick"]["BANKNIFTY"]["price"], 54291.35)
        hub.offline = os.path.join(LIVE, "nowhere")
        self.assertEqual((hub.tick(), hub.tick()), (2, 4))                # backs off when Yahoo fails


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dash = load("trader-india", "dashboard")
        cls.hub = cls.dash.Hub(LIVE, "2026-10-27 12:10", "/nonexistent")
        cls.hub.tick()
        cls.hub.fast()
        cls.hub.slow()
        cls.hub.daily()

    def test_market_and_short_term_in_state(self):
        s = self.hub.state()
        self.assertEqual(s["market"]["regime"], "bearish")
        self.assertEqual(s["daily_scan"]["done"], 11)
        sw = {r["symbol"]: r for r in s["swing"]}
        self.assertEqual(len(sw), 10)
        self.assertEqual(sw["ETERNAL"]["check"], "BLOCK")
        self.assertEqual(sw["BEL"]["event"]["date"], "2026-10-30")
        self.assertEqual(s["swing"][0]["symbol"], "ETERNAL")
        bel = next(r for r in s["setups"] if r["symbol"] == "BEL")
        self.assertEqual((bel["daily_trend"], bel["score"]), ("downtrend", 3))
        self.assertIn("market regime bearish: a long fights the bigger trend", bel["flags"])
        self.assertEqual(s["nifty_pct"], 0.01)

    def test_daily_chart(self):
        c = self.hub.chart("BEL", "1d")
        self.assertEqual((c["tf"], len(c["bars"])), ("1d", 248))
        self.assertEqual((c["bars"][-1]["sma20"], c["bars"][-1]["sma200"]), (376.66, 416.27))
        self.assertIsNone(c["bars"][0]["sma20"])

    def test_state(self):
        s = self.hub.state()
        self.assertEqual(s["errors"], {})
        self.assertEqual((s["session"], s["cutoff"], s["live_session"]), ("2026-10-27", "12:10", True))
        self.assertEqual(s["scan"]["done"], 10)
        self.assertEqual(s["options"]["NIFTY"]["expiry"], "03-Nov-2026")
        self.assertEqual(s["options"]["NIFTY"]["expiry_today"], "27-Oct-2026")
        self.assertEqual(s["options"]["BANKNIFTY"]["lot"], 30)
        self.assertEqual(s["options"]["NIFTY"]["setup"]["action"], "NO TRADE")
        self.assertEqual(s["setups"][0]["symbol"], "BEL")
        self.assertEqual(len(s["setups"]), 10)
        self.assertNotIn("bars", s)
        self.assertNotIn("chains", s)

    def test_capital_and_max_loss_resize(self):
        s = self.hub.state(capital=200000, max_loss=2000)
        bel = next(r for r in s["setups"] if r["symbol"] == "BEL")
        self.assertEqual(bel["qty"], 421)
        self.assertLessEqual(bel["risk"], 2000)

    def test_chart(self):
        c = self.hub.chart("BEL")
        self.assertEqual((c["day"], len(c["bars"])), ("2026-10-27", 36))   # 35 complete + the one forming
        self.assertEqual(c["bars"][-2]["vwap"], 370.97)
        self.assertIsNone(self.hub.chart("NOPE"))
        self.assertIsNone(self.hub.chart("NIFTY")["bars"][-1]["vwap"])   # Yahoo's indices have no volume

    def test_server_answers_on_localhost(self):
        import threading
        import urllib.request
        srv = self.dash.ThreadingHTTPServer(("127.0.0.1", 0), self.dash.make_handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b"<title>NSE Desk</title>", r.read())
            with urllib.request.urlopen(base + "/api/state?capital=50000&max_loss=500") as r:
                self.assertEqual(json.loads(r.read().decode())["max_loss"], 500)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/chart?symbol=NOPE")
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_no_order_code(self):
        for f in ("dashboard.py", "dashboard.html"):
            with open(os.path.join(os.path.dirname(LIVE), "..", "..", "..", "trader-india", f)) as fh:
                src = fh.read().lower()
            for word in ("place_order", "placeorder", "kite.trade", "/orders"):
                self.assertNotIn(word, src, f)


def run_tech(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tech.main(argv)
    return code, out.getvalue(), err.getvalue()


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
if "SLOW" in args[1]:
    time.sleep(30)
print(json.dumps({"type": "system", "subtype": "init"}))
print("not json, ignored")
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Bash", "input": {"command": "python3 india.py clock --live", "description": "NSE clock"}}]}}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "WebSearch", "input": {"query": "GIFT Nifty today"}}]}}))
fail = "FAIL" in args[1]
print(json.dumps({"type": "result", "is_error": fail, "result": "## Report\n| a | b |\n|---|---|\n| 1 | 2 |\n" + json.dumps(args),
                  "total_cost_usd": 0.42, "num_turns": 5}))
'''


class Routine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dash = load("trader-india", "dashboard")
        cls.tmp = tempfile.mkdtemp()
        cls.fake = os.path.join(cls.tmp, "claude")
        with open(cls.fake, "w") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(cls.fake, 0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def wait(self, runner, secs=10):
        import time
        t = time.time()
        while runner.snapshot()["status"] == "running" and time.time() - t < secs:
            time.sleep(0.05)
        return runner.snapshot()

    def test_default_prompt_is_the_routine(self):
        p = self.dash.DEFAULT_PROMPT
        self.assertTrue(p.startswith("/trader-india Run my full Indian intraday routine"))
        self.assertIn("Capital Rs 50,000 and max loss Rs 500", p)
        self.assertIn("Do not place any order.", p)

    def test_run_collects_steps_result_and_cost(self):
        r = self.dash.Runner(self.fake, self.tmp)
        r.start("")   # empty -> the default prompt
        s = self.wait(r)
        self.assertEqual(s["status"], "done")
        again = self.dash.Runner(self.fake, self.tmp)   # a restart keeps the last report
        self.assertEqual((again.snapshot()["status"], again.snapshot()["result"]), ("done", s["result"]))
        self.assertEqual(s["steps"], ["Bash: NSE clock", "WebSearch: GIFT Nifty today"])
        self.assertEqual((s["cost"], s["turns"]), (0.42, 5))
        self.assertIn("## Report", s["result"])
        args = json.loads(s["result"].splitlines()[-1])
        self.assertEqual(args[0], "-p")
        self.assertEqual(args[1], self.dash.DEFAULT_PROMPT)
        self.assertEqual(args[2:5], ["--output-format", "stream-json", "--verbose"])
        allowed = args[args.index("--allowedTools") + 1:]
        self.assertEqual(allowed, self.dash.ALLOWED_TOOLS)
        self.assertFalse(any(t.startswith(("Edit", "Write", "mcp")) for t in allowed))

    def test_events_command_flags_today(self):
        code, out, _ = run_tech(["events", "--offline", LIVE, "--now", "2026-10-28 10:00", "--days", "1"])
        self.assertEqual(code, 0)
        self.assertIn("HINDUNILVR  2026-10-28  Financial Results/Dividend", out)
        self.assertIn("TODAY: results-day swings, no intraday entry", out)
        self.assertIn("TRENT       2026-10-29", out)
        self.assertNotIn("BEL ", out)

    def test_failed_run(self):
        r = self.dash.Runner(self.fake, self.tmp, save=None)
        r.start("FAIL please")
        s = self.wait(r)
        self.assertEqual((s["status"], s["cost"]), ("error", 0.42))

    def test_one_run_at_a_time_and_stop(self):
        r = self.dash.Runner(self.fake, self.tmp)
        r.start("SLOW")
        with self.assertRaises(RuntimeError):
            r.start("again")
        self.assertTrue(r.stop())
        s = self.wait(r)
        self.assertEqual((s["status"], s["error"]), ("error", "stopped"))

    def test_no_claude(self):
        with self.assertRaises(RuntimeError) as cm:
            self.dash.Runner(None).start("x")
        self.assertIn("not found", str(cm.exception))

    def test_only_the_dashboard_page_can_start_a_run(self):
        import threading
        import urllib.request
        hub = self.dash.Hub(LIVE, "2026-10-28 09:40", "/nonexistent")
        runner = self.dash.Runner(self.fake, self.tmp)
        srv = self.dash.ThreadingHTTPServer(("127.0.0.1", 0), self.dash.make_handler(hub, runner))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        base = "http://127.0.0.1:%d" % port

        def post(origin):
            req = urllib.request.Request(base + "/api/run", data=b'{"prompt": "hi"}', method="POST",
                                         headers={"Content-Type": "application/json", "Origin": origin})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
        try:
            self.assertEqual(post("https://evil.example"), 403)
            self.assertEqual(runner.snapshot()["status"], "idle")
            self.assertEqual(post(base), 202)
            self.assertEqual(self.wait(runner)["status"], "done")
            with urllib.request.urlopen(base + "/api/run") as resp:
                s = json.loads(resp.read().decode())
            self.assertEqual(s["default_prompt"], self.dash.DEFAULT_PROMPT)
            self.assertEqual(s["steps"][0], "Bash: NSE clock")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_find_claude_prefers_the_env(self):
        old = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = self.fake
        try:
            self.assertEqual(self.dash.find_claude(), self.fake)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_BIN")
            else:
                os.environ["CLAUDE_BIN"] = old


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
