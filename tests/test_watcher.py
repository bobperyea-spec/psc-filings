import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psc_watcher as w  # noqa: E402


def make_page(rows, total=None, base="/dl/"):
    """Build an ERF-style results page. Each row is a dict; see ROWS below."""
    trs = []
    for r in rows:
        if r.get("lock"):
            desc = f'<img src="images/lock.gif" alt="confidential"> {r["title"]}'
            chk = ""
        else:
            icon = {"pdf": "pdficon.gif", "xlsx": "xlsicon.gif", "html": "htmicon.gif"}[r["kind"]]
            desc = (f'<img src="images/{icon}"><a href="{base}{r["ref"]}">{r["title"]}</a> '
                    f'<i>({r["size"]:,} KB)</i>')
            chk = '<input type="checkbox"> Include'
        trs.append(
            f'<tr><td>{chk}</td><td>{r["ref"]}</td><td>{desc}</td><td>{r["type"]}</td>'
            f'<td><a href="/docket?id={r["docket"]}">{r["docket"]}</a></td>'
            f'<td>{r["received"]}</td><td><a href="#">{r["by"]}</a></td>'
            f'<td><a href="/details?ref={r["ref"]}"><img src="d.gif"></a></td></tr>'
        )
    total = len(rows) if total is None else total
    return f"""<html><body>
    <div>Search Criteria Docket ID: - Date Range: 10/19/2026 - 10/26/2026</div>
    <div>Access to <img src="lock.gif"> - confidential</div>
    <span>Total Return: {total:,} rows</span>
    <table id="grid">
      <tr><th>Include in Download</th><th><a>PSC Ref</a></th><th><a>Description</a></th>
          <th><a>Document Type</a></th><th><a>Docket or Utility</a></th>
          <th><a>Received Date</a></th><th><a>Filed By</a></th><th>Details</th></tr>
      {''.join(trs)}
    </table></body></html>"""


ROWS = [
    dict(ref=616204, title="3135-WR-102 Service List", kind="pdf", size=3, type="Mailing / Service List",
         docket="3135-WR-102", received="10/23/2026 03:35:00 PM", by="PSC Records Management Unit"),
    dict(ref=616201, title="Final Decision Signed and Served 10/23/2026", kind="pdf", size=117, type="Order",
         docket="6680-GP-2026", received="10/23/2026 03:15:00 PM", by="Test Filer"),
    dict(ref=616198, title="5-BS-280 Asset Purchase Agreement (REDACTED COPY)", kind="pdf", size=19851,
         type="Contract", docket="5-BS-280", received="10/23/2026 03:04:00 PM", by="Test Filer"),
    dict(ref=616197, title="5-BS-280 Asset Purchase Agreement_Executed CONFIDENTIAL", lock=True, size=0,
         type="Contract", docket="5-BS-280", received="10/23/2026 03:04:00 PM", by="Test Filer"),
    dict(ref=616190, title="Project 185 Speed Tiers and Pricing.xlsx - upload with status report", kind="xlsx",
         size=20, type="Report", docket="5-CPF-2023", received="10/23/2026 01:56:00 PM", by="Test Filer"),
    dict(ref=616189, title="Public Comment by A. Resident", kind="html", size=5, type="Comments",
         docket="9823-CE-100", received="10/23/2026 12:54:00 AM", by="Public Comment"),
    dict(ref=616179, title="Ex.-ALJ-All Leases <Part 150> </script><b>x", kind="pdf", size=11313,
         type="Exhibit - Offered", docket="9836-CE-100", received="10/23/2026 01:28:00 PM", by="Test Filer"),
]


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.total = w.parse_results(make_page(ROWS), "https://example.test/ERF/results.aspx")
        self.by_ref = {r["psc_ref"]: r for r in self.rows}

    def test_counts_match(self):
        self.assertEqual(self.total, 7)
        self.assertEqual(len(self.rows), 7)

    def test_basic_fields(self):
        r = self.by_ref[616201]
        self.assertEqual(r["description"], "Final Decision Signed and Served 10/23/2026")
        self.assertEqual(r["doc_type"], "Order")
        self.assertEqual(r["docket"], "6680-GP-2026")
        self.assertEqual(r["filed_by"], "Test Filer")
        self.assertEqual(r["size_kb"], 117)
        self.assertEqual(r["url"], "https://example.test/dl/616201")
        self.assertEqual(r["docket_url"], "https://example.test/docket?id=6680-GP-2026")
        self.assertEqual(r["details_url"], "https://example.test/details?ref=616201")

    def test_large_size_with_comma(self):
        self.assertEqual(self.by_ref[616198]["size_kb"], 19851)

    def test_confidential_row_has_no_link(self):
        r = self.by_ref[616197]
        self.assertEqual(r["confidential"], 1)
        self.assertIsNone(r["url"])

    def test_file_kinds(self):
        self.assertEqual(self.by_ref[616190]["file_kind"], "xlsx")
        self.assertEqual(self.by_ref[616189]["file_kind"], "html")
        self.assertEqual(self.by_ref[616204]["file_kind"], "pdf")

    def test_dates_including_12am_and_12pm(self):
        self.assertEqual(self.by_ref[616204]["received"], "2026-10-23 15:35:00")
        self.assertEqual(self.by_ref[616189]["received"], "2026-10-23 00:54:00")
        self.assertEqual(w.parse_received("10/23/2026 12:05:00 PM"), "2026-10-23 12:05:00")

    def test_mismatch_is_detectable(self):
        rows, total = w.parse_results(make_page(ROWS, total=822))
        self.assertEqual(total, 822)
        self.assertEqual(len(rows), 7)

    def test_no_table_returns_empty(self):
        rows, total = w.parse_results("<html><body>Total Return: 0 rows</body></html>")
        self.assertEqual(rows, [])


class DbAndViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = w.connect(self.dir)
        self.rows, _ = w.parse_results(make_page(ROWS), "https://example.test/x")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_is_idempotent(self):
        self.assertEqual(w.upsert_rows(self.conn, self.rows), 7)
        self.assertEqual(w.upsert_rows(self.conn, self.rows), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 7)

    def test_upsert_keeps_download_state(self):
        w.upsert_rows(self.conn, self.rows)
        self.conn.execute("UPDATE filings SET status='downloaded', local_path='files/a.pdf' WHERE psc_ref=616201")
        self.conn.commit()
        w.upsert_rows(self.conn, self.rows)
        r = self.conn.execute("SELECT status, local_path FROM filings WHERE psc_ref=616201").fetchone()
        self.assertEqual((r["status"], r["local_path"]), ("downloaded", "files/a.pdf"))

    def test_viewer_builds_and_escapes_script_tags(self):
        w.upsert_rows(self.conn, self.rows)
        cfg = json.loads(json.dumps(w.DEFAULTS))
        cfg["viewer_days"] = 100000
        out, n = w.build_viewer(self.conn, cfg, self.dir)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(n, 7)
        # the hostile description must not be able to close the data script block:
        # the template has exactly two script blocks (data + page code)
        self.assertEqual(text.count("</script>"), 2)
        start = text.index('<script id="data"')
        blob = text[text.index(">", start) + 1: text.index("</script>", start)]
        data = json.loads(blob.replace("<\\/", "</"))
        self.assertEqual(len(data["rows"]), 7)
        self.assertIn("UR", data["types"])


class CaseCodeTests(unittest.TestCase):
    def test_parse_docket_id(self):
        self.assertEqual(w.parse_docket_id("6680-UR-124"), ("6680", "UR", "124"))
        self.assertEqual(w.parse_docket_id("5-BS-280"), ("5", "BS", "280"))
        self.assertEqual(w.parse_docket_id("6680-GP-2026"), ("6680", "GP", "2026"))
        self.assertEqual(w.parse_docket_id("5-CPF-2023"), ("5", "CPF", "2023"))     # three-letter code
        self.assertIsNone(w.parse_docket_id("6680"))                                  # utility id only
        self.assertIsNone(w.parse_docket_id(""))

    def test_docket_page_url_matches_psc_pattern(self):
        self.assertEqual(
            w.docket_page_url("6680-UR-124"),
            "https://apps.psc.wi.gov/APPS/dockets/content/detail.aspx?case=UR&id=6680&num=124")

    def test_table_covers_codes_seen_on_erf_and_has_no_duplicates(self):
        codes = [c for c, _, _ in w.CASE_TYPES]
        self.assertEqual(len(codes), len(set(codes)))
        for code in ("UR", "GP", "CE", "WR", "WQ", "BS", "CPF", "PGA", "RX", "ER", "GR"):
            self.assertIn(code, codes)

    def test_labels(self):
        by = {c: w.case_label(d, g) for c, d, g in w.CASE_TYPES}
        self.assertEqual(by["UR"], "Combined Utility rates")
        self.assertEqual(by["GP"], "Gas Supply Plan")
        self.assertEqual(by["CE"], "Electric construction")
        self.assertEqual(by["BS"], "Buy / Sell")
        self.assertEqual(by["SB"], "Stocks & Bonds")
        self.assertEqual(by["PA"], "Purchase Audit (Gas Supply Review)")
        self.assertEqual(by["WQ"], "Water Quick: Simplified Rate Case")


DOCKET_LAYOUTS = {
    "table": """<table><tr><td>Docket Number</td><td>6680-UR-124</td></tr>
        <tr><td>Status</td><td>Active</td></tr>
        <tr><td>Title</td><td>Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates</td></tr>
        <tr><td>Applicant(s)</td><td>Wisconsin Power And Light Company (6680)</td></tr>
        <tr><td>Case Coordinator</td><td>Test Coordinator</td></tr></table>""",
    "labels": """<div><span class="lbl">Docket Number:</span><span>6680-UR-124</span></div>
        <div><span class="lbl">Title:</span><span>Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates</span></div>
        <div><span class="lbl">Status:</span><span>Active</span></div>
        <div><span class="lbl">Applicant(s):</span><span>Wisconsin Power And Light Company (6680)</span></div>
        <div><span class="lbl">Case Coordinator:</span><span>Test Coordinator</span></div>""",
    "inline": """<p><b>Title:</b> Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates</p>
        <p>Status: Active</p><p>Applicant(s): Wisconsin Power And Light Company (6680)</p>
        <p>Case Coordinator: Test Coordinator</p>""",
}
EXPECTED_TITLE = "Application of Wisconsin Power and Light Company for Authority to Adjust Electric and Natural Gas Rates"


class DocketPageTests(unittest.TestCase):
    def test_parses_each_layout(self):
        for name, body in DOCKET_LAYOUTS.items():
            with self.subTest(layout=name):
                info = w.parse_docket_page(f"<html><body><script>var Title='x'</script>{body}</body></html>")
                self.assertEqual(info.get("title"), EXPECTED_TITLE)
                self.assertEqual(info.get("status"), "Active")
                self.assertEqual(info.get("applicant"), "Wisconsin Power And Light Company (6680)")
                self.assertEqual(info.get("coordinator"), "Test Coordinator")

    def test_first_title_wins_over_shorter_repeat(self):
        body = DOCKET_LAYOUTS["table"] + "<table><tr><td>Title</td><td>Wisconsin Power and Light Company for Authority</td></tr></table>"
        self.assertEqual(w.parse_docket_page(body)["title"], EXPECTED_TITLE)

    def test_label_with_empty_value_is_not_filled_with_next_label(self):
        info = w.parse_docket_page("<table><tr><td>Title</td><td></td></tr><tr><td>Status</td><td>Closed</td></tr></table>")
        self.assertNotIn("title", info)
        self.assertEqual(info["status"], "Closed")

    def test_page_without_fields(self):
        self.assertEqual(w.parse_docket_page("<html><body>Page not found</body></html>"), {})


class _DocketHandler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        _DocketHandler.hits.append(self.path)
        if "broken" in self.path:
            body, code = b"<html><body>Server error</body></html>", 200
        elif "gone" in self.path:
            body, code = b"nope", 404
        else:
            body, code = f"<html><body>{DOCKET_LAYOUTS['table']}</body></html>".encode(), 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class UpdateDocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _DocketHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _DocketHandler.hits.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = w.connect(self.dir)
        self.cfg = json.loads(json.dumps(w.DEFAULTS))
        self.cfg.update(delay_seconds=0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def fetch(url):
        from urllib.error import HTTPError
        try:
            with urlopen(url) as r:
                return r.status, r.read(), {}
        except HTTPError as e:
            return e.code, b"", {}

    def add(self, dockets):
        """Insert filings for the given dockets, and point each docket's page at the local server."""
        rows = []
        for i, d in enumerate(dockets):
            rows.append(dict(psc_ref=700000 + i, description="x", doc_type="Order", docket=d,
                             received="2026-10-23 10:00:00", filed_by="t", file_kind="pdf", size_kb=1,
                             url=None, docket_url=None, details_url=None, confidential=0))
        w.upsert_rows(self.conn, rows)
        for d in dockets:
            p = w.parse_docket_id(d)
            if p:
                self.conn.execute("INSERT OR IGNORE INTO dockets(docket, util_id, case_code, seq, url) VALUES(?,?,?,?,?)",
                                  (d, p[0], p[1], p[2], f"http://127.0.0.1:{self.port}/{d}"))
        self.conn.commit()

    def title(self, d):
        return self.conn.execute("SELECT title, status, attempts FROM dockets WHERE docket=?", (d,)).fetchone()

    def test_names_dockets_once(self):
        self.add(["6680-UR-124", "5-BS-280"])
        self.assertEqual(w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None), 2)
        self.assertEqual(self.title("6680-UR-124")["title"], EXPECTED_TITLE)
        self.assertEqual(self.title("5-BS-280")["status"], "Active")
        _DocketHandler.hits.clear()
        self.assertEqual(w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None), 0)
        self.assertEqual(_DocketHandler.hits, [])                       # no refetch for already-named dockets

    def test_utility_only_ids_are_not_dockets(self):
        self.add(["6680"])
        w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM dockets").fetchone()[0], 0)

    def test_failures_are_counted_and_capped(self):
        self.add(["1-UR-1-broken".replace("-broken", ""), ])
        self.conn.execute("UPDATE dockets SET url=?", (f"http://127.0.0.1:{self.port}/broken",))
        self.conn.commit()
        for _ in range(8):
            w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None)
        self.assertEqual(self.title("1-UR-1")["attempts"], 5)           # stops at 5 tries
        n = len(_DocketHandler.hits)
        w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None)
        self.assertEqual(len(_DocketHandler.hits), n)
        # --retry-titles resets the counter
        w.update_dockets(self.conn, self.fetch, self.cfg, retry=True, log=lambda *_: None)
        self.assertEqual(self.title("1-UR-1")["attempts"], 1)

    def test_per_run_cap_and_newest_first(self):
        self.add([f"{n}-UR-1" for n in range(10, 15)])
        self.conn.execute("UPDATE filings SET received='2026-10-20 09:00:00'")
        self.conn.execute("UPDATE filings SET received='2026-10-24 09:00:00' WHERE docket='14-UR-1'")
        self.conn.commit()
        self.cfg["max_docket_fetches"] = 2
        w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None)
        self.assertEqual(len(_DocketHandler.hits), 2)
        self.assertIn("/14-UR-1", _DocketHandler.hits[0])               # newest activity first

    def test_stops_after_four_misses_in_a_row(self):
        self.add([f"{n}-UR-1" for n in range(20, 30)])
        self.conn.execute("UPDATE dockets SET url=?", (f"http://127.0.0.1:{self.port}/gone",))
        self.conn.commit()
        w.update_dockets(self.conn, self.fetch, self.cfg, log=lambda *_: None)
        self.assertEqual(len(_DocketHandler.hits), 4)


class SiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = w.connect(self.dir)
        rows, _ = w.parse_results(make_page(ROWS + [dict(
            ref=616300, title="Public Comment by Jane Q. Resident", kind="html", size=5, type="Comments",
            docket="9823-CE-100", received="10/23/2026 01:54:00 PM", by="Public Comment")]), "https://example.test/x")
        w.upsert_rows(self.conn, rows)
        self.conn.execute("UPDATE filings SET local_path='files/x.pdf' WHERE psc_ref=616201")
        self.conn.execute("INSERT OR IGNORE INTO dockets(docket, util_id, case_code, seq, url, title, status) "
                          "VALUES('6680-GP-2026','6680','GP','2026','https://example.test/d','Gas Supply Plan for 2026','Active')")
        self.conn.commit()
        self.cfg = json.loads(json.dumps(w.DEFAULTS))
        self.cfg.update(viewer_days=100000, site_dir=str(self.dir / "site"), custom_domain="psc.example.com")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def payload(self):
        return json.loads((self.dir / "site" / "data.json").read_text())

    def test_site_files_and_data(self):
        out, n = w.build_viewer(self.conn, self.cfg, self.dir, site=True)
        self.assertEqual(out, self.dir / "site" / "index.html")
        self.assertEqual(n, 8)
        self.assertIn('<script id="data" type="application/json"></script>', out.read_text())  # data is in data.json
        self.assertEqual((self.dir / "site" / "CNAME").read_text().strip(), "psc.example.com")
        p = self.payload()
        self.assertEqual(p["dockets"]["6680-GP-2026"]["n"], "Gas Supply Plan for 2026")
        self.assertEqual(p["types"]["GP"]["l"], "Gas Supply Plan")
        by = {r["r"]: r for r in p["rows"]}
        self.assertIsNone(by[616201]["l"])                               # no local file links on the public site

    def test_public_comment_names_are_masked_on_site_only(self):
        w.build_viewer(self.conn, self.cfg, self.dir, site=True)
        by = {r["r"]: r for r in self.payload()["rows"]}
        self.assertEqual(by[616300]["d"], "Public comment (name withheld)")
        self.assertNotIn("Jane", json.dumps(self.payload()))
        out, _ = w.build_viewer(self.conn, self.cfg, self.dir, site=False)  # local viewer keeps full text
        self.assertIn("Jane Q. Resident", out.read_text())
        self.cfg["mask_public_comments"] = False
        w.build_viewer(self.conn, self.cfg, self.dir, site=True)
        self.assertIn("Jane Q. Resident", json.dumps(self.payload()))

    def test_confidential_rows_are_never_linked(self):
        w.build_viewer(self.conn, self.cfg, self.dir, site=True)
        by = {r["r"]: r for r in self.payload()["rows"]}
        self.assertIsNone(by[616197]["u"])
        self.assertEqual(by[616197]["c"], "confidential")


class _Handler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        ref = int(self.path.rsplit("/", 1)[1])
        _Handler.hits.append(ref)
        if ref == 616201:
            body, ctype = b"%PDF-1.4\n" + b"x" * 2000, "application/pdf"
        elif ref == 616190:
            body, ctype = b"PK\x03\x04" + b"x" * 500, "application/octet-stream"
        elif ref == 616204:
            body, ctype = b"<html><body>Session expired</body></html>", "text/html"  # pdf expected
        elif ref == 616189:
            body, ctype = b"<html><body>comment text</body></html>", "text/html"
        else:
            body, ctype = b"%PDF-1.4 big", "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.hits.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = w.connect(self.dir)
        base = f"http://127.0.0.1:{self.port}/dl/"
        rows, _ = w.parse_results(make_page(ROWS, base=base), f"http://127.0.0.1:{self.port}/x")
        w.upsert_rows(self.conn, rows)
        self.cfg = json.loads(json.dumps(w.DEFAULTS))
        self.cfg.update(delay_seconds=0, max_download_mb=5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def fetch(url):
        with urlopen(url) as r:
            return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}

    def status(self, ref):
        return self.conn.execute("SELECT status, local_path FROM filings WHERE psc_ref=?", (ref,)).fetchone()

    def test_download_rules(self):
        done, skipped, failed = w.download_pending(self.conn, self.fetch, self.cfg, self.dir, log=lambda *_: None)
        self.assertEqual((done, skipped, failed), (3, 2, 1))
        ok = self.status(616201)
        self.assertEqual(ok["status"], "downloaded")
        self.assertTrue((self.dir / ok["local_path"]).read_bytes().startswith(b"%PDF"))
        self.assertTrue(self.status(616190)["local_path"].endswith(".xlsx"))
        self.assertTrue(self.status(616189)["local_path"].endswith(".html"))
        self.assertEqual(self.status(616204)["status"], "failed")        # HTML instead of PDF
        self.assertEqual(self.status(616198)["status"], "skipped_size")   # 19 MB over 5 MB cap
        self.assertEqual(self.status(616179)["status"], "skipped_size")
        self.assertEqual(self.status(616197)["status"], "confidential")
        self.assertNotIn(616198, _Handler.hits)                           # never requested
        self.assertNotIn(616197, _Handler.hits)

    def test_forced_download_ignores_cap(self):
        done, _, _ = w.download_pending(self.conn, self.fetch, self.cfg, self.dir, refs=[616198], log=lambda *_: None)
        self.assertEqual(done, 1)
        self.assertEqual(self.status(616198)["status"], "downloaded")

    def test_failed_downloads_stop_retrying_after_three_attempts(self):
        for _ in range(5):
            w.download_pending(self.conn, self.fetch, self.cfg, self.dir, log=lambda *_: None)
        self.assertEqual(self.conn.execute("SELECT attempts FROM filings WHERE psc_ref=616204").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
