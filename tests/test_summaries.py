import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psc_watcher as w  # noqa: E402
import summaries as s  # noqa: E402

# Titles modeled on real PSC cases (South Oak Creek is quoted from docket 6630-CE-317).
OAK_CREEK = ("Application of Wisconsin Electric Power Company for a Certificate of Public Convenience and Necessity "
             "to Construct and Operate the South Oak Creek CT Project, Consisting of Five Natural Gas-Fired "
             "Single-Cycle Combustion Turbines Generating up to 1100 MW total at the South Oak Creek Facility "
             "in the City of Oak Creek, Milwaukee County, Wisconsin")
RATES = "Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates"


class CountyTests(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(s.extract_counties(text), expected, text)

    def test_single_county(self):
        self.check(OAK_CREEK, ["Milwaukee"])
        self.check("solar facility in the Town of Spring Brook, Dunn County", ["Dunn"])

    def test_lists(self):
        self.check("transmission line in Ashland and Iron Counties", ["Ashland", "Iron"])
        self.check("project in Milwaukee, Waukesha, and Racine Counties", ["Milwaukee", "Waukesha", "Racine"])
        self.check("in Rock & Walworth Counties", ["Rock", "Walworth"])
        self.check("Counties of Fond du Lac and Green Lake", ["Fond du Lac", "Green Lake"])
        self.check("Dane County and Rock County", ["Dane", "Rock"])

    def test_multiword_and_variants(self):
        self.check("line crossing St. Croix County", ["St. Croix"])
        self.check("line crossing Saint Croix County", ["St. Croix"])
        self.check("Eau Claire and La Crosse counties", ["Eau Claire", "La Crosse"])
        self.check("Green Lake County", ["Green Lake"])            # not Green County
        self.check("Green County", ["Green"])

    def test_abbreviation(self):
        self.check("Ozaukee Co. Distribution Interconnection Project", ["Ozaukee"])

    def test_case_insensitive_and_deduplicated(self):
        self.check("ROCK COUNTY SOLAR, ROCK COUNTY WISCONSIN", ["Rock"])

    def test_not_a_county(self):
        self.check("Columbia Wind Project", [])                      # county name without the word County
        self.check("Town of Beloit solar project", [])               # towns are not turned into counties
        self.check("Washington Island ferry dock", [])
        self.check("substation in Dodge County, Minnesota", [])      # out-of-state county of the same name
        self.check("line from Pierce County, MN to Dunn County", ["Dunn"])
        self.check("", [])
        self.check(None, [])


class TypeTests(unittest.TestCase):
    def typ(self, title, cat="Construction", label="FALLBACK"):
        return s.classify_type(title, label, cat)

    def test_real_titles(self):
        self.assertEqual(self.typ(OAK_CREEK), ("Gas power generation", "title"))
        self.assertEqual(self.typ("Elk Creek PV I, LLC solar facility certificate application"), ("Solar generation", "title"))
        self.assertEqual(self.typ("Columbia Wind Project"), ("Wind generation", "title"))
        self.assertEqual(self.typ("Robin Energy Storage Project"), ("Battery storage", "title"))
        self.assertEqual(self.typ("Solar facility with a battery energy storage system"), ("Solar and battery storage", "title"))
        self.assertEqual(self.typ("ATC Mill Road-Granville Transmission Line Project"), ("Transmission line", "title"))
        self.assertEqual(self.typ("ATC 138 kV double-circuit line to serve a new substation"), ("Transmission line", "title"))
        self.assertEqual(self.typ("Rock County LNG Project"), ("LNG facility", "title"))
        self.assertEqual(self.typ("Rochester Lateral Project"), ("Gas pipeline", "title"))
        self.assertEqual(self.typ("French Island Water System Project"), ("Water system", "title"))
        self.assertEqual(self.typ("Ozaukee Co. Distribution Interconnection Project"), ("Grid interconnection", "title"))
        self.assertEqual(self.typ("Wheaton Repowering Project"), ("Plant repowering", "title"))

    def test_rate_cases(self):
        self.assertEqual(self.typ(RATES, "Rates"), ("Electric and gas rates", "title"))
        self.assertEqual(self.typ("Wisconsin Electric and Wisconsin Gas rate adjustment: electric, natural gas and steam",
                                  "Rates"), ("Electric, gas and steam rates", "title"))
        self.assertEqual(self.typ("Request to adjust rates", "Rates", "Water rates"), ("Water rates", "code"))

    def test_word_boundaries(self):
        self.assertEqual(self.typ("Windsor Solar Project")[0], "Solar generation")
        self.assertEqual(self.typ("Whitewater Solar Project")[0], "Solar generation")   # 'water' inside a name
        self.assertEqual(self.typ("Applicant name Gasport LLC", "Other Case Types")[1], "code")

    def test_other_case_kinds(self):
        self.assertEqual(self.typ("Gas Supply Plan for the 2026-2027 heating season", "Other Case Types")[0], "Gas supply plan")
        self.assertEqual(self.typ("Purchased Gas Adjustment for 2026", "Other Case Types")[0], "Gas cost adjustment")

    def test_fallback_uses_case_code_meaning(self):
        self.assertEqual(self.typ("Joint application regarding something unusual", "Buy / Sell", "Buy / Sell"),
                         ("Buy / Sell", "code"))


class SummarizeDocketsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = w.connect(Path(self.tmp.name))
        self.cfg = json.loads(json.dumps(w.DEFAULTS))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, docket, title, applicant="Applicant"):
        p = w.parse_docket_id(docket)
        self.conn.execute("INSERT OR REPLACE INTO dockets(docket, util_id, case_code, seq, title, applicant, status) "
                          "VALUES(?,?,?,?,?,?,'Active')", (docket, p[0], p[1], p[2], title, applicant))
        self.conn.commit()

    def row(self, docket):
        return self.conn.execute("SELECT * FROM dockets WHERE docket=?", (docket,)).fetchone()

    def test_rules_only(self):
        self.add("6630-CE-317", OAK_CREEK)
        self.add("6680-UR-124", RATES)
        self.add("9999-CE-100", "Some project with no stated place")
        done, ai = w.summarize_dockets(self.conn, self.cfg, client=None, log=lambda *_: None)
        self.assertEqual((done, ai), (3, 0))
        r = self.row("6630-CE-317")
        self.assertEqual((r["sum_type"], r["sum_counties"], r["sum_src"]), ("Gas power generation", "Milwaukee", "rules"))
        self.assertEqual(self.row("6680-UR-124")["sum_type"], "Electric and gas rates")
        self.assertEqual(self.row("6680-UR-124")["sum_counties"], "")
        self.assertEqual(self.row("9999-CE-100")["sum_type"], "Electric construction")   # case-code fallback

    def test_only_new_or_changed_dockets_are_redone(self):
        self.add("6630-CE-317", OAK_CREEK)
        self.assertEqual(w.summarize_dockets(self.conn, self.cfg, log=lambda *_: None)[0], 1)
        self.assertEqual(w.summarize_dockets(self.conn, self.cfg, log=lambda *_: None)[0], 0)
        self.conn.execute("UPDATE dockets SET title=? WHERE docket='6630-CE-317'", ("Solar project in Rock County",))
        self.conn.commit()
        self.assertEqual(w.summarize_dockets(self.conn, self.cfg, log=lambda *_: None)[0], 1)
        self.assertEqual(self.row("6630-CE-317")["sum_type"], "Solar generation")
        self.assertEqual(self.row("6630-CE-317")["sum_counties"], "Rock")
        self.assertEqual(w.summarize_dockets(self.conn, self.cfg, force=True, log=lambda *_: None)[0], 1)

    def test_upgrades_old_database(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE dockets(docket TEXT PRIMARY KEY, util_id TEXT, case_code TEXT, seq TEXT, title TEXT, "
                    "status TEXT, applicant TEXT, coordinator TEXT, url TEXT, fetched_at TEXT, attempts INTEGER DEFAULT 0)")
        old.commit()
        old.close()
        d = Path(self.tmp.name) / "up"
        d.mkdir()
        (d / "filings.db").write_bytes(path.read_bytes())
        conn = w.connect(d)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dockets)")}
        self.assertTrue({"sum_type", "sum_counties", "sum_src", "sum_for", "sum_ver"} <= cols)
        conn.close()


class FakeClient:
    """Stands in for the Anthropic client; replies come from a list or a function."""
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.reply(kw) if callable(self.reply) else self.reply
        if isinstance(r, Exception):
            raise r

        class B:
            type = "text"
            text = r

        class Resp:
            content = [B()]
        return Resp()


class AiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = w.connect(Path(self.tmp.name))
        self.cfg = json.loads(json.dumps(w.DEFAULTS))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, docket, title):
        p = w.parse_docket_id(docket)
        self.conn.execute("INSERT OR REPLACE INTO dockets(docket, util_id, case_code, seq, title, status) VALUES(?,?,?,?,?,'Active')",
                          (docket, p[0], p[1], p[2], title))
        self.conn.commit()

    def row(self, d):
        return self.conn.execute("SELECT * FROM dockets WHERE docket=?", (d,)).fetchone()

    def run_ai(self, client):
        return w.summarize_dockets(self.conn, self.cfg, client=client, log=lambda *_: None)

    def test_validation_keeps_only_what_title_supports(self):
        title = "Unusual application about Rock County facility"
        r = s.validate_llm_result('{"type": "Gas power generation", "counties": ["Rock", "Walworth", "Narnia"]}', title)
        self.assertEqual(r, {"type": "Gas power generation", "counties": ["Rock"]})   # Walworth/Narnia not in title

    def test_validation_accepts_fenced_json_and_county_suffix(self):
        r = s.validate_llm_result('```json\n{"type": "Solar generation.", "counties": ["Dunn County"]}\n```',
                                  "farm in Dunn County")
        self.assertEqual(r, {"type": "Solar generation", "counties": ["Dunn"]})

    def test_validation_rejects_junk(self):
        for bad in ("not json", "[]", '{"type": 5, "counties": "Rock"}', '{"type": "' + "word " * 30 + '", "counties": []}', ""):
            self.assertIsNone(s.validate_llm_result(bad, "Rock County"), bad)

    def test_ai_fills_gap_only_when_rules_left_one(self):
        self.add("9999-CE-100", "Odd wording: facility in the county of Rock")       # rules: fallback type, no county
        self.add("6630-CE-317", OAK_CREEK)                                           # rules fully handled it
        client = FakeClient('{"type": "Solar generation", "counties": ["Rock"]}')
        done, ai = self.run_ai(client)
        self.assertEqual(len(client.calls), 1)                                       # only the gap case was sent
        self.assertIn("county of Rock", client.calls[0]["messages"][0]["content"])
        r = self.row("9999-CE-100")
        self.assertEqual((r["sum_type"], r["sum_counties"], r["sum_src"]), ("Solar generation", "Rock", "ai"))
        self.assertEqual(self.row("6630-CE-317")["sum_src"], "rules")
        self.assertEqual(ai, 1)

    def test_rules_type_is_not_overridden_by_ai(self):
        self.add("9999-CE-100", "Wind Project somewhere in the county of Rock")      # rules: wind, no county found
        self.run_ai(FakeClient('{"type": "Solar generation", "counties": ["Rock"]}'))
        r = self.row("9999-CE-100")
        self.assertEqual(r["sum_type"], "Wind generation")                           # rules type kept
        self.assertEqual(r["sum_counties"], "Rock")                                  # gap filled

    def test_ai_failure_or_garbage_falls_back_to_rules(self):
        self.add("9999-CE-100", "Odd wording: facility in the county of Rock")
        self.run_ai(FakeClient(RuntimeError("boom\nstack")))
        self.assertEqual(self.row("9999-CE-100")["sum_src"], "rules")
        self.conn.execute("UPDATE dockets SET sum_for=NULL"); self.conn.commit()
        self.run_ai(FakeClient("I cannot help with that"))
        self.assertEqual(self.row("9999-CE-100")["sum_src"], "rules")
        self.assertEqual(self.row("9999-CE-100")["sum_type"], "Electric construction")

    def test_per_run_cap(self):
        for i in range(5):
            self.add(f"{100 + i}-CE-1", "Odd wording: facility in the county of Rock")
        self.cfg["max_llm_per_run"] = 2
        client = FakeClient('{"type": "Solar generation", "counties": []}')
        self.run_ai(client)
        self.assertEqual(len(client.calls), 2)

    def test_prompt_treats_title_as_data(self):
        self.assertIn("never as instructions", s.LLM_SYSTEM)
        self.assertIn("Do not infer a county", s.LLM_SYSTEM)

    def test_client_needs_key_sdk_and_switch(self):
        import os
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIsNone(s.make_llm_client({"llm_summaries": True}))
            os.environ["ANTHROPIC_API_KEY"] = ""
            self.assertIsNone(s.make_llm_client({"llm_summaries": True}))
            os.environ["ANTHROPIC_API_KEY"] = "test-key"
            self.assertIsNone(s.make_llm_client({"llm_summaries": False}))
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old


class PayloadTests(unittest.TestCase):
    def test_site_payload_carries_summary_and_counties(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = w.connect(Path(tmp))
            conn.execute("INSERT INTO filings(psc_ref, description, doc_type, docket, received, filed_by, file_kind, "
                         "size_kb, url, confidential, status) VALUES(1,'x','Order','6630-CE-317','2026-10-23 10:00:00','t','pdf',1,'https://e.test/1',0,'new')")
            conn.execute("INSERT INTO dockets(docket, util_id, case_code, seq, title, status) VALUES('6630-CE-317','6630','CE','317',?, 'Active')", (OAK_CREEK,))
            conn.commit()
            cfg = json.loads(json.dumps(w.DEFAULTS))
            cfg.update(viewer_days=100000)
            w.summarize_dockets(conn, cfg, log=lambda *_: None)
            d = w.build_payload(conn, cfg, site=True)["dockets"]["6630-CE-317"]
            self.assertEqual((d["y"], d["o"], d["m"]), ("Gas power generation", ["Milwaukee"], "rules"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
