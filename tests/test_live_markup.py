"""Tests built from what the live PSC pages looked like on Oct 25, 2026 (checked in Chrome):
- ERF search form: <input type="date" id="datepicker_start|end">, submit button "GO" (#btn_search)
  next to a decoy "Clear Search Criteria" button (#btn_reset)
- results table: <th> headers, relative links, icons pdficon.gif / xls.png / html.png / lock.png
- "Total Return: 0" (no word 'rows') on a day with no filings
- docket page: <span id="lbl_docket_title"><span>DOCKET</span> (Status)<br>TITLE</span>, no 'Title:' label
"""
import argparse
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psc_watcher as w  # noqa: E402

RESULT_BASE = "https://apps.psc.wi.gov/ERF/ERFsearch/content/searchResult.aspx"


def erf_row(ref, desc, size, icon, doc_type, docket, received, by, lock=False):
    if lock:
        d = f'<img src="/_images/lock.png" alt="confidential"> {desc}'
        inc = ""
    else:
        d = (f'<img src="/_images/{icon}" alt="Download File"><a href="/pages/viewdoc.htm?docid={ref}">{desc}</a> '
             f'<i>({size:,} KB)</i>')
        inc = '<input type="checkbox"> Include'
    return (f'<tr><td>{inc}</td><td>{ref}</td><td>{d}</td><td>{doc_type}</td>'
            f'<td><a href="http://apps.psc.wi.gov/pages/docketDetail.htm?dockt_id={docket}">{docket}</a></td>'
            f'<td>{received}</td><td><a href="https://apps.psc.wi.gov/pages/ERFsubmitter.htm?docid={ref}">{by}</a></td>'
            f'<td><a href="documentInfo.aspx?docid={ref}"><img src="/_images/detail.png" alt="uploaded detail"></a></td></tr>')


LIVE_ROWS = [
    erf_row(616204, "3135-WR-102 Service List", 3, "pdficon.gif", "Mailing / Service List", "3135-WR-102",
            "10/23/2026 03:35:00 PM", "PSC Records Management Unit"),
    erf_row(616190, "Project 185 Speed Tiers.xlsx - upload with status report", 20, "xls.png", "Report",
            "5-CPF-2023", "10/23/2026 01:56:00 PM", "Test Filer"),
    erf_row(616189, "Public Comment by Test Resident", 5, "html.png", "Comments", "9823-CE-100",
            "10/23/2026 01:54:00 PM", "Public Comment"),
    erf_row(616197, "5-BS-280 Asset Purchase Agreement_Executed CONFIDENTIAL", 0, "", "Contract", "5-BS-280",
            "10/23/2026 03:04:00 PM", "Test Filer", lock=True),
]


def live_page(rows, total_text=None):
    total_text = total_text or f"Total Return: {len(rows)} rows"
    return f"""<html><body>
    <div>Search Criteria Docket ID: - Date Range: 10/23/2026 - 10/24/2026</div>
    <div>{total_text}</div>
    <table><tr><th>Include in Download</th><th><a>PSC Ref</a></th><th><a>Description</a></th><th><a>Document Type</a></th>
    <th><a>Docket or Utility</a></th><th><a>Received Date</a></th><th><a>Filed By</a></th><th>Details</th></tr>
    {''.join(rows)}</table></body></html>"""


ZERO_PAGE = ("<html><body>Search Criteria Docket ID: - Date Range: 10/24/2026 - 10/26/2026 "
             "There were no filings that match your search criteria Total Return: 0   No data was returned.</body></html>")

TITLE = "Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates"
LIVE_DOCKET_PAGE = f"""<html><body>
<table><tr><td class="bar_header">Case Management System(CMS) :: Docket Information</td></tr>
<tr><td><span id="lbl_docket_title"><span>6680-UR-124</span> (Active)<br>{TITLE}</span></td></tr>
<tr><td>Select a tab to display additional information.</td></tr></table>
<table><tr><th colspan="2"><b>Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates</b></th></tr>
<tr><td class="bar_title">Applicant(s):</td><td>Wisconsin Power And Light Company (6680)</td></tr>
<tr><td class="bar_title">Case Coordinator:</td><td>Kain Coronado</td></tr></table></body></html>"""


class LiveResultsMarkup(unittest.TestCase):
    def setUp(self):
        self.rows, self.total = w.parse_results(live_page(LIVE_ROWS), RESULT_BASE)
        self.by = {r["psc_ref"]: r for r in self.rows}

    def test_counts_and_fields(self):
        self.assertEqual((self.total, len(self.rows)), (4, 4))
        r = self.by[616204]
        self.assertEqual((r["doc_type"], r["docket"], r["filed_by"], r["size_kb"]),
                         ("Mailing / Service List", "3135-WR-102", "PSC Records Management Unit", 3))
        self.assertEqual(r["received"], "2026-10-23 15:35:00")

    def test_links_resolve_against_the_results_page(self):
        r = self.by[616204]
        self.assertEqual(r["url"], "https://apps.psc.wi.gov/pages/viewdoc.htm?docid=616204")
        self.assertEqual(r["details_url"], "https://apps.psc.wi.gov/ERF/ERFsearch/content/documentInfo.aspx?docid=616204")

    def test_icons(self):
        self.assertEqual(self.by[616204]["file_kind"], "pdf")
        self.assertEqual(self.by[616190]["file_kind"], "xlsx")
        self.assertEqual(self.by[616189]["file_kind"], "html")

    def test_padlock_rows_are_confidential_and_unlinked(self):
        r = self.by[616197]
        self.assertEqual((r["confidential"], r["url"]), (1, None))
        self.assertEqual(sum(x["confidential"] for x in self.rows), 1)


class ZeroResults(unittest.TestCase):
    def test_zero_page_is_a_valid_empty_result(self):
        self.assertEqual(w.parse_results(ZERO_PAGE, RESULT_BASE), ([], 0))

    def test_total_pattern_handles_both_wordings(self):
        for text, n in (("Total Return: 245 rows", 245), ("Total Return: 0   ", 0), ("Total Return: 1,204 rows", 1204)):
            self.assertEqual(int(w._TOTAL_RE.search(text).group(1).replace(",", "")), n)


class LiveDocketMarkup(unittest.TestCase):
    def test_reads_title_status_applicant_coordinator(self):
        info = w.parse_docket_page(LIVE_DOCKET_PAGE)
        self.assertEqual(info["title"], TITLE)                      # the full title, not the shorter repeat
        self.assertEqual(info["status"], "Active")
        self.assertEqual(info["applicant"], "Wisconsin Power And Light Company (6680)")
        self.assertEqual(info["coordinator"], "Kain Coronado")

    def test_other_statuses_and_long_titles(self):
        page = LIVE_DOCKET_PAGE.replace("(Active)", "(Closed)")
        self.assertEqual(w.parse_docket_page(page)["status"], "Closed")
        long_title = ("Application of Wisconsin Electric Power Company for a Certificate of Public Convenience and Necessity "
                      "to Construct and Operate the South Oak Creek CT Project in the City of Oak Creek, Milwaukee County, Wisconsin")
        page = LIVE_DOCKET_PAGE.replace(TITLE, long_title)
        self.assertEqual(w.parse_docket_page(page)["title"], long_title)


# ---- end to end: the real run() code path in a real browser against a local copy of the ERF form ----

FORM = """<html><body><form method="get" action="/result">
<input type="number" id="docket_util_id" name="docket_util_id">
<input type="text" id="docket_case_type_cd" name="docket_case_type_cd">
<input type="date" id="datepicker_start" name="datepicker_start">
<input type="date" id="datepicker_end" name="datepicker_end">
<input type="submit" id="btn_search" name="btn_search" value="GO">
<input type="submit" id="btn_reset" name="btn_reset" value="Clear Search Criteria">
</form></body></html>"""


class FakeErf(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/search":
            body = FORM
        elif u.path == "/result":
            FakeErf.seen.append({k: v[0] for k, v in q.items()})
            if "btn_reset" in q:
                body = "<html><body>Search criteria cleared</body></html>"       # wrong button: no results line
            elif q.get("datepicker_start", [""])[0] in ("2026-10-24", "2026-10-25"):
                body = ZERO_PAGE                                                 # the weekend
            else:
                body = live_page(LIVE_ROWS)
        else:
            body = "<html><body>not found</body></html>"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _chromium_ok():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_chromium_ok(), "Playwright's Chromium is not installed")
class EndToEndRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeErf)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeErf.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **over):
        cfg = json.loads(json.dumps(w.DEFAULTS))
        cfg.update(data_dir=str(self.dir / "data"), site_dir=str(self.dir / "site"), viewer_days=100000,
                   search_url=f"http://127.0.0.1:{self.port}/search", max_docket_fetches=0,
                   results_timeout_seconds=15, llm_summaries=False)
        cfg.update(over)
        return cfg

    def args(self, since, until):
        return argparse.Namespace(since=since, until=until, headed=False, site=True, retry_titles=False)

    def test_run_uses_iso_dates_and_the_go_button_and_builds_the_site(self):
        w.cmd_run(self.args("2026-10-23", "2026-10-24"), self.cfg())
        self.assertEqual(len(FakeErf.seen), 1)
        sent = FakeErf.seen[0]
        self.assertEqual((sent["datepicker_start"], sent["datepicker_end"]), ("2026-10-23", "2026-10-24"))
        self.assertIn("btn_search", sent)
        self.assertNotIn("btn_reset", sent)
        conn = w.connect(self.dir / "data")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 4)
        data = json.loads((self.dir / "site" / "data.json").read_text())
        self.assertEqual(len(data["rows"]), 4)
        conn.close()

    def test_quiet_weekend_is_not_an_error_and_keeps_existing_data(self):
        w.cmd_run(self.args("2026-10-23", "2026-10-24"), self.cfg())
        w.cmd_run(self.args("2026-10-24", "2026-10-26"), self.cfg())          # zero filings: must not raise
        data = json.loads((self.dir / "site" / "data.json").read_text())
        self.assertEqual(len(data["rows"]), 4)                                # earlier filings still on the site

    def test_auto_detection_also_finds_the_right_fields_and_skips_the_clear_button(self):
        cfg = self.cfg(selectors={"start": "", "end": "", "submit": ""})
        w.cmd_run(self.args("2026-10-23", "2026-10-24"), cfg)
        sent = FakeErf.seen[0]
        self.assertEqual(sent["datepicker_start"], "2026-10-23")
        self.assertNotIn("btn_reset", sent)


class DonateAndDisclaimer(unittest.TestCase):
    def build(self, site, donate_text):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(json.dumps(w.DEFAULTS))
            cfg.update(data_dir=d, site_dir=str(Path(d) / "site"))
            conn = w.connect(d)
            f = w.HERE / "donate.html"
            old = f.read_text() if f.exists() else None
            try:
                f.write_text(donate_text)
                out, _ = w.build_viewer(conn, cfg, d, site=site)
                return Path(out).read_text()
            finally:
                f.write_text(old) if old is not None else f.unlink()
                conn.close()

    def test_disclaimer_is_always_shown(self):
        for site in (True, False):
            self.assertIn("can't guarantee the accuracy", self.build(site, ""))

    def test_donate_code_on_site_only(self):
        html = self.build(True, "<!-- note --><div id=\"pp\">PAYPAL</div>")
        self.assertIn('<div id="pp">PAYPAL</div>', html)
        self.assertNotIn("note -->", html)
        self.assertNotIn("__DONATE", html)
        self.assertNotIn("PAYPAL", self.build(False, "<div>PAYPAL</div>"))

    def test_empty_donate_file_hides_section(self):
        self.assertRegex(self.build(True, "<!-- nothing -->"), r'id="donate"\s+hidden')


if __name__ == "__main__":
    unittest.main()
