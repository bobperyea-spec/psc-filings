#!/usr/bin/env python3
"""
PSC Wisconsin ERF watcher.

Pulls recent filings from the Public Service Commission's Electronic Records
Filing (ERF) system, stores them in SQLite, downloads the public documents, and
builds a searchable HTML viewer.

Commands
  run       search ERF for recent filings, store them, download new files, rebuild viewer
  ingest    parse a saved ERF results page (HTML file) into the database, no network
  download  fetch specific filings by PSC Ref (ignores the size cap)
  build     rebuild the viewer from the database
  probe     save the search form's fields and a screenshot, for fixing selectors
"""
import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import summaries

ERF_SEARCH_URL = "https://apps.psc.wi.gov/ERF/ERFsearch/default.aspx"
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "data_dir": "data",
    "lookback_days": 2,
    "download": True,
    "max_download_mb": 5,
    "delay_seconds": 1.5,
    "viewer_days": 120,
    "watch_terms": ["Beloit", "Rock County", "6680-"],
    "headed": False,
    "max_docket_fetches": 80,        # case-name lookups per run (each is one page fetch)
    "site_dir": "site",              # where `run --site` writes the public website
    "custom_domain": "",             # e.g. "psc.example.com" (writes a CNAME file for GitHub Pages)
    "mask_public_comments": True,    # public website only: hide names of people who file public comments
    "llm_summaries": True,           # use the Anthropic API for gaps, only if ANTHROPIC_API_KEY is set
    "llm_model": "claude-haiku-5-5",
    "max_llm_per_run": 60,
    "search_url": ERF_SEARCH_URL,
    "results_timeout_seconds": 240,  # how long to wait for the ERF results page
    # CSS selectors for the ERF search form. These are the real field ids, checked against the live
    # page on Oct 25, 2026 (the dates are <input type="date"> boxes; the search button is labelled GO).
    # If the PSC redesigns the page, run `probe` and update them.
    "selectors": {"start": "#datepicker_start", "end": "#datepicker_end", "submit": "#btn_search"},
}


class ProbeNeeded(RuntimeError):
    pass


# --------------------------------------------------------------------------
# config / database
# --------------------------------------------------------------------------

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    p = Path(path)
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def data_path(cfg):
    d = Path(cfg["data_dir"])
    if not d.is_absolute():
        d = HERE / d
    d.mkdir(parents=True, exist_ok=True)
    return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS filings(
  psc_ref      INTEGER PRIMARY KEY,
  description  TEXT,
  doc_type     TEXT,
  docket       TEXT,
  received     TEXT,
  filed_by     TEXT,
  file_kind    TEXT,
  size_kb      INTEGER,
  url          TEXT,
  docket_url   TEXT,
  details_url  TEXT,
  confidential INTEGER DEFAULT 0,
  local_path   TEXT,
  status       TEXT DEFAULT 'new',
  attempts     INTEGER DEFAULT 0,
  first_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_received ON filings(received);
CREATE INDEX IF NOT EXISTS idx_docket ON filings(docket);
CREATE TABLE IF NOT EXISTS dockets(
  docket      TEXT PRIMARY KEY,
  util_id     TEXT,
  case_code   TEXT,
  seq         TEXT,
  title       TEXT,
  status      TEXT,
  applicant   TEXT,
  coordinator TEXT,
  url         TEXT,
  fetched_at  TEXT,
  attempts    INTEGER DEFAULT 0,
  sum_type    TEXT,
  sum_counties TEXT,
  sum_src     TEXT,
  sum_for     TEXT,
  sum_ver     INTEGER
);
"""

NEW_DOCKET_COLUMNS = {"sum_type": "TEXT", "sum_counties": "TEXT", "sum_src": "TEXT",
                      "sum_for": "TEXT", "sum_ver": "INTEGER"}


def connect(data_dir):
    conn = sqlite3.connect(str(Path(data_dir) / "filings.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(dockets)")}
    for col, typ in NEW_DOCKET_COLUMNS.items():      # upgrade databases created by earlier versions
        if col not in have:
            conn.execute(f"ALTER TABLE dockets ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def upsert_rows(conn, rows):
    """Insert or refresh rows. Returns the number of filings not seen before."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    existing = {r[0] for r in conn.execute("SELECT psc_ref FROM filings")}
    new = 0
    for r in rows:
        if r["psc_ref"] not in existing:
            new += 1
        status = "confidential" if r["confidential"] else ("new" if r["url"] else "no_link")
        conn.execute(
            """
            INSERT INTO filings(psc_ref, description, doc_type, docket, received, filed_by,
                                file_kind, size_kb, url, docket_url, details_url,
                                confidential, status, first_seen)
            VALUES(:psc_ref,:description,:doc_type,:docket,:received,:filed_by,
                   :file_kind,:size_kb,:url,:docket_url,:details_url,
                   :confidential,:status,:first_seen)
            ON CONFLICT(psc_ref) DO UPDATE SET
              description=excluded.description, doc_type=excluded.doc_type,
              docket=excluded.docket, received=excluded.received,
              filed_by=excluded.filed_by, file_kind=excluded.file_kind,
              size_kb=excluded.size_kb,
              url=COALESCE(excluded.url, filings.url),
              docket_url=COALESCE(excluded.docket_url, filings.docket_url),
              details_url=COALESCE(excluded.details_url, filings.details_url),
              confidential=excluded.confidential,
              status=CASE WHEN filings.status IN ('confidential','no_link')
                           AND excluded.url IS NOT NULL THEN 'new'
                          ELSE filings.status END
            """,
            {**r, "status": status, "first_seen": now},
        )
    conn.commit()
    return new


# --------------------------------------------------------------------------
# parsing the ERF results page
# --------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


_SIZE_RE = re.compile(r"\(\s*([\d,]+)\s*KB\s*\)", re.I)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)", re.I)
# "Total Return: 245 rows" when there are filings; "Total Return: 0" (no "rows") on a day with none.
_TOTAL_RE = re.compile(r"Total Return:\s*([\d,]+)(?:\s*rows)?", re.I)


def parse_received(text):
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mo, d, y, h, mi, s, ap = m.groups()
    h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {h:02d}:{mi}:{s}"


def _kind_from_cell(cell, link_text):
    """Work out the file type from the icon (src/alt/title) or the file name."""
    hints = " ".join(
        ((i.get("src") or "") + " " + (i.get("alt") or "") + " " + (i.get("title") or ""))
        for i in cell.find_all("img")
    ).lower()
    for needle, kind in (("pdf", "pdf"), ("xls", "xlsx"), ("excel", "xlsx"),
                         ("doc", "docx"), ("word", "docx"), ("htm", "html"), ("html", "html")):
        if needle in hints:
            return kind
    m = re.search(r"\.(pdf|xlsx?|docx?|html?|zip|csv|txt)\b", link_text or "", re.I)
    if m:
        ext = m.group(1).lower()
        return {"xls": "xlsx", "doc": "docx", "htm": "html"}.get(ext, ext)
    return "unknown"


def parse_results(html_text, base_url=ERF_SEARCH_URL):
    """Return (rows, total_reported). total_reported is None if the page has no count."""
    soup = BeautifulSoup(html_text, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    m = _TOTAL_RE.search(page_text)
    total = int(m.group(1).replace(",", "")) if m else None

    header_tr = None
    for tr in soup.find_all("tr"):
        names = [_norm(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]
        if "psc ref" in names and any(n.startswith("description") for n in names):
            header_tr = tr
            break
    if header_tr is None:
        return [], total

    heads = [_norm(c.get_text(" ", strip=True)) for c in header_tr.find_all(["th", "td"], recursive=False)]

    def col(*prefixes):
        for i, h in enumerate(heads):
            if any(h.startswith(p) for p in prefixes):
                return i
        return None

    c_ref = col("psc ref")
    c_desc = col("description")
    c_type = col("document type")
    c_dock = col("docket")
    c_recv = col("received")
    c_by = col("filed by")
    c_det = col("details")
    if None in (c_ref, c_desc, c_recv):
        return [], total

    table = header_tr.find_parent("table")
    rows = []
    for tr in table.find_all("tr"):
        if tr is header_tr or tr.find_parent("table") is not table:
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) <= max(c_ref, c_desc, c_recv):
            continue
        ref_txt = re.sub(r"\D", "", cells[c_ref].get_text(" ", strip=True))
        if not ref_txt:
            continue

        dcell = cells[c_desc]
        anchor = next((a for a in dcell.find_all("a") if a.get("href")), None)
        raw_title = (anchor.get_text(" ", strip=True) if anchor else dcell.get_text(" ", strip=True))
        size_m = _SIZE_RE.search(dcell.get_text(" ", strip=True))
        title = _SIZE_RE.sub("", raw_title).strip()
        href = anchor.get("href").strip() if anchor else None
        url = None
        if href and not href.lower().startswith(("javascript:", "#")):
            url = urljoin(base_url, href)

        icons = " ".join(
            ((i.get("src") or "") + " " + (i.get("alt") or "") + " " + (i.get("title") or ""))
            for i in dcell.find_all("img")
        ).lower()
        restricted = ("lock" in icons or "confiden" in icons or "camera" in icons
                      or "incamera" in icons or "in camera" in icons)

        docket_text, docket_url = "", None
        if c_dock is not None and c_dock < len(cells):
            dc = cells[c_dock]
            docket_text = dc.get_text(" ", strip=True)
            da = dc.find("a", href=True)
            if da and not da["href"].lower().startswith(("javascript:", "#")):
                docket_url = urljoin(base_url, da["href"])

        details_url = None
        if c_det is not None and c_det < len(cells):
            ta = cells[c_det].find("a", href=True)
            if ta and not ta["href"].lower().startswith(("javascript:", "#")):
                details_url = urljoin(base_url, ta["href"])

        rows.append({
            "psc_ref": int(ref_txt),
            "description": title,
            "doc_type": cells[c_type].get_text(" ", strip=True) if c_type is not None else "",
            "docket": docket_text,
            "received": parse_received(cells[c_recv].get_text(" ", strip=True)),
            "filed_by": cells[c_by].get_text(" ", strip=True) if c_by is not None else "",
            "file_kind": _kind_from_cell(dcell, title),
            "size_kb": int(size_m.group(1).replace(",", "")) if size_m else None,
            "url": None if restricted else url,
            "docket_url": docket_url,
            "details_url": details_url,
            "confidential": 1 if restricted else 0,
        })
    return rows, total


# --------------------------------------------------------------------------
# case codes and case names
# --------------------------------------------------------------------------

# Official PSC case type table (snapshot of the Case Type Lookup page, Oct 2026):
# [code, description, category]. Codes are usually two letters, a few are one or three.
CASE_TYPES = json.loads((HERE / "case_types.json").read_text(encoding="utf-8"))
_UTILITY_WORDS = {"Electric", "Gas", "Heat", "Sewer", "Telephone", "Water", "Combined Utility"}
_DOCKET_RE = re.compile(r"^(\d+)-([A-Z]+)-(\d+)$")


def case_label(desc, cat):
    """Plain-English meaning of a case type, e.g. ('Combined Utility', 'Rates') -> 'Combined Utility rates'."""
    if not cat or cat == "Other Case Types" or cat == desc:
        return desc
    if desc == "All":
        return cat
    if cat.lower().startswith(desc.lower()):
        return cat
    if desc in _UTILITY_WORDS:
        return f"{desc} {cat.lower()}"
    return f"{desc}: {cat}"


def parse_docket_id(text):
    """'6680-UR-124' -> ('6680', 'UR', '124'); None if it is not a docket number."""
    m = _DOCKET_RE.match((text or "").strip().upper())
    return m.groups() if m else None


def docket_page_url(docket):
    p = parse_docket_id(docket)
    if not p:
        return None
    return f"https://apps.psc.wi.gov/APPS/dockets/content/detail.aspx?case={p[1]}&id={p[0]}&num={p[2]}"


_FIELD_LABELS = {
    "title": ("title",),
    "status": ("status",),
    "applicant": ("applicants", "applicant"),
    "coordinator": ("case coordinator", "docket coordinator"),
}
_ALL_LABELS = {lab for labs in _FIELD_LABELS.values() for lab in labs} | {"docket number", "docket"}


def _label_key(text):
    return re.sub(r"\(s\)", "s", (text or "").lower()).rstrip(":").strip()


def parse_docket_page(html_text):
    """Pull title, status, applicant and case coordinator from a docket detail page.

    The real page (checked Oct 25, 2026) puts the docket, status and title in one element:
        <span id="lbl_docket_title"><span>6680-UR-124</span> (Active)<br>Application of ...</span>
    with no "Title:" or "Status:" labels; applicant and coordinator are label/value table cells.
    Label/value layouts (a label line then a value line, or 'Label: value') are handled as a fallback.
    Returns only the fields it finds.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    out = {}
    head = soup.find(id="lbl_docket_title")
    if head is not None:
        m = re.match(r"^\d+-[A-Za-z]+-\d+\s*\(([^)]*)\)\s*(.*)$", re.sub(r"\s+", " ", head.get_text(" ", strip=True)))
        if m:
            if m.group(1).strip():
                out["status"] = m.group(1).strip()
            if m.group(2).strip():
                out["title"] = m.group(2).strip()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln]
    for i, line in enumerate(lines):
        key = _label_key(line)
        for field, labels in _FIELD_LABELS.items():
            if field in out:
                continue
            if key in labels:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt and _label_key(nxt) not in _ALL_LABELS:
                    out[field] = nxt
                continue
            for lab in labels:
                m = re.match(rf"^{re.escape(lab)}(?:\(s\))?\s*:\s*(.+)$", line, re.I)
                if m:
                    out[field] = m.group(1).strip()
                    break
    return out


def update_dockets(conn, fetch, cfg, retry=False, log=print):
    """Register new dockets and look up their case names from the PSC docket pages.

    Titles rarely change, so each docket is fetched once (retried up to 5 times if the
    page could not be read). Status is refreshed every 14 days for dockets still receiving filings.
    Returns the number of dockets named this run.
    """
    now = dt.datetime.now()
    for (d,) in conn.execute("SELECT DISTINCT docket FROM filings WHERE docket <> ''").fetchall():
        p = parse_docket_id(d)
        if p:
            conn.execute("INSERT OR IGNORE INTO dockets(docket, util_id, case_code, seq, url) VALUES(?,?,?,?,?)",
                         (d.upper(), p[0], p[1], p[2], docket_page_url(d)))
    if retry:
        conn.execute("UPDATE dockets SET attempts=0 WHERE title IS NULL OR title=''")
    conn.commit()
    recent = (now - dt.timedelta(days=3)).strftime("%Y-%m-%d")
    stale = (now - dt.timedelta(days=14)).isoformat(timespec="seconds")
    todo = conn.execute(
        """
        SELECT d.docket, d.url FROM dockets d
        WHERE (d.title IS NULL OR d.title = '') AND d.attempts < 5
        ORDER BY (SELECT MAX(received) FROM filings f WHERE f.docket = d.docket) DESC
        """).fetchall()
    todo += conn.execute(
        """
        SELECT d.docket, d.url FROM dockets d
        WHERE d.title <> '' AND d.fetched_at < ?
          AND EXISTS (SELECT 1 FROM filings f WHERE f.docket = d.docket AND f.received >= ?)
        """, (stale, recent)).fetchall()
    named = misses = 0
    for row in todo[: cfg["max_docket_fetches"]]:
        time.sleep(cfg["delay_seconds"])
        try:
            status, body, _ = fetch(row["url"])
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            info = parse_docket_page(body.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            log(f"  could not read docket page for {row['docket']}: {exc}")
            info = {}
        if info.get("title"):
            conn.execute(
                "UPDATE dockets SET title=?, status=?, applicant=?, coordinator=?, fetched_at=?, attempts=0 WHERE docket=?",
                (info["title"], info.get("status"), info.get("applicant"), info.get("coordinator"),
                 now.isoformat(timespec="seconds"), row["docket"]))
            named += 1
            misses = 0
        else:
            conn.execute("UPDATE dockets SET attempts=attempts+1 WHERE docket=?", (row["docket"],))
            misses += 1
        conn.commit()
        if misses >= 4:
            log("  four docket pages in a row gave no title; stopping case-name lookups for this run")
            break
    return named


# --------------------------------------------------------------------------
# case summaries (project type and counties)
# --------------------------------------------------------------------------

def summarize_dockets(conn, cfg, client=None, force=False, log=print):
    """Fill in a short summary for each named docket that doesn't have a current one.

    Rules run for every docket. If an AI client is given, it is used only where the rules left a gap
    (no project type, or a county word in the title the rules couldn't read), up to max_llm_per_run
    calls, and its answer is checked against the title before use.
    Returns (summarized, ai_used).
    """
    cats = {c: (d, g) for c, d, g in CASE_TYPES}
    rows = conn.execute("SELECT docket, case_code, title, applicant, sum_for, sum_ver FROM dockets "
                        "WHERE title IS NOT NULL AND title <> ''").fetchall()
    done = ai_used = 0
    budget = cfg.get("max_llm_per_run", 60)
    for r in rows:
        if not force and r["sum_for"] == r["title"] and r["sum_ver"] == summaries.SUMMARY_VERSION:
            continue
        desc, cat = cats.get(r["case_code"], (None, None))
        label = case_label(desc, cat) if desc else f"Code {r['case_code']}"
        res = summaries.summarize_rules(r["title"], label, cat)
        typ, counties, src = res["type"], list(res["counties"]), "rules"
        if client is not None and budget > 0 and summaries.needs_llm(res, r["title"]):
            budget -= 1
            try:
                ai = summaries.llm_summarize(client, cfg.get("llm_model", "claude-haiku-5-5"),
                                             r["title"], r["applicant"], label)
            except Exception as exc:  # noqa: BLE001 - fall back to the rules result
                log(f"  AI summary failed for {r['docket']}: {str(exc).splitlines()[0][:120]}")
                ai = None
            if ai:
                used = False
                if res["type_src"] == "code" and ai["type"]:
                    typ, used = ai["type"], True
                if not counties and ai["counties"]:
                    counties, used = ai["counties"], True
                if used:
                    src = "ai"
                    ai_used += 1
        conn.execute("UPDATE dockets SET sum_type=?, sum_counties=?, sum_src=?, sum_for=?, sum_ver=? WHERE docket=?",
                     (typ, ",".join(counties), src, r["title"], summaries.SUMMARY_VERSION, r["docket"]))
        done += 1
    conn.commit()
    return done, ai_used


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def safe_name(s, n=70):
    s = re.sub(r"[^\w.\- ]+", "_", s or "").strip(" ._")
    return re.sub(r"\s+", "_", s)[:n] or "file"


_CT_EXT = {"application/pdf": "pdf", "text/html": "html", "text/plain": "txt",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
           "application/vnd.ms-excel": "xls", "application/zip": "zip", "text/csv": "csv",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}


def pick_ext(row, headers):
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m and "." in m.group(1):
        return re.sub(r"\W", "", m.group(1).rsplit(".", 1)[1].lower())[:5] or "bin"
    if row["file_kind"] and row["file_kind"] != "unknown":
        return row["file_kind"]
    return _CT_EXT.get(headers.get("content-type", "").split(";")[0].strip().lower(), "bin")


def looks_wrong(kind, body):
    """True if the response is clearly not the file we asked for (error page, login page)."""
    head = body[:512].lstrip().lower()
    if kind == "pdf" and not body.startswith(b"%PDF"):
        return True
    if kind in ("xlsx", "docx", "zip") and not body.startswith(b"PK"):
        return True
    if kind not in ("html", "txt", "csv") and (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
        return True
    return False


def download_pending(conn, fetch, cfg, data_dir, refs=None, log=print):
    """Download filings that have a link and no local copy yet.

    fetch(url) -> (status_code, body_bytes, headers_dict_lowercase_keys)
    With refs given, the size cap is ignored for those filings.
    """
    max_kb = cfg["max_download_mb"] * 1024
    if refs:
        marks = ",".join("?" * len(refs))
        todo = conn.execute(
            f"SELECT * FROM filings WHERE psc_ref IN ({marks}) AND url IS NOT NULL AND confidential=0",
            list(refs)).fetchall()
    else:
        todo = conn.execute(
            "SELECT * FROM filings WHERE url IS NOT NULL AND confidential=0 "
            "AND status IN ('new','failed','skipped_size') AND attempts < 3 "
            "ORDER BY received DESC").fetchall()
    done = skipped = failed = 0
    for row in todo:
        if not refs and row["size_kb"] and row["size_kb"] > max_kb:
            conn.execute("UPDATE filings SET status='skipped_size' WHERE psc_ref=?", (row["psc_ref"],))
            skipped += 1
            continue
        time.sleep(cfg["delay_seconds"])
        try:
            status, body, headers = fetch(row["url"])
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            if not body:
                raise RuntimeError("empty response")
            if looks_wrong(row["file_kind"], body):
                raise RuntimeError("response was not the expected file type")
        except Exception as exc:  # noqa: BLE001 - record and keep going
            conn.execute("UPDATE filings SET status='failed', attempts=attempts+1 WHERE psc_ref=?",
                         (row["psc_ref"],))
            conn.commit()
            log(f"  failed {row['psc_ref']}: {exc}")
            failed += 1
            continue
        ext = pick_ext(row, headers)
        rel = Path("files") / safe_name(row["docket"] or "misc", 40) / \
            f"{row['psc_ref']}_{safe_name(Path(row['description']).stem if row['description'] else '')}.{ext}"
        dest = Path(data_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(dest)
        conn.execute("UPDATE filings SET status='downloaded', local_path=? WHERE psc_ref=?",
                     (rel.as_posix(), row["psc_ref"]))
        conn.commit()
        done += 1
    conn.commit()
    return done, skipped, failed


# --------------------------------------------------------------------------
# viewer and website
# --------------------------------------------------------------------------

_PUBLIC_COMMENT_RE = re.compile(r"^\s*public comment by\b", re.I)


def _json_for_script(obj):
    """JSON that is safe inside a <script> block (no early </script>, no HTML comments)."""
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    return s.replace("</", "<\\/").replace("<!--", "<\\!--")


def build_payload(conn, cfg, site=False):
    cutoff = (dt.date.today() - dt.timedelta(days=cfg["viewer_days"])).isoformat()
    mask = site and cfg.get("mask_public_comments", True)
    rows = conn.execute(
        "SELECT * FROM filings WHERE received >= ? ORDER BY received DESC, psc_ref DESC", (cutoff,)
    ).fetchall()
    data = []
    for r in rows:
        desc = r["description"]
        if mask and (_PUBLIC_COMMENT_RE.match(desc or "") or (r["filed_by"] or "").strip().lower() == "public comment"):
            desc = "Public comment (name withheld)"
        url = r["url"]
        if not url and not r["confidential"]:
            url = f"https://apps.psc.wi.gov/ERF/ERFview/viewdoc.aspx?docid={r['psc_ref']}"
        data.append({
            "r": r["psc_ref"], "d": desc, "t": r["doc_type"], "k": r["docket"],
            "w": r["received"], "f": r["filed_by"], "s": r["size_kb"], "u": url,
            "l": None if site else r["local_path"], "c": r["status"], "x": r["file_kind"],
        })
    used = {r["docket"] for r in rows if r["docket"]}
    dockets = {}
    for d in conn.execute("SELECT * FROM dockets WHERE title IS NOT NULL AND title <> ''"):
        if d["docket"] in used:
            dockets[d["docket"]] = {"n": d["title"], "s": d["status"], "a": d["applicant"],
                                    "c": d["coordinator"], "u": d["url"], "y": d["sum_type"],
                                    "o": [c for c in (d["sum_counties"] or "").split(",") if c],
                                    "m": d["sum_src"]}
    types = {c: {"d": desc, "g": cat, "l": case_label(desc, cat)} for c, desc, cat in CASE_TYPES}
    meta = {"generated": dt.datetime.now().strftime("%b %d, %Y %I:%M %p") + " Central",
            "watch": cfg.get("watch_terms", [])}
    return {"meta": meta, "types": types, "dockets": dockets, "rows": data}


def _with_donate(html, site):
    """Fill the donate footer from donate.html (public site only). No code, or a local viewer: section hidden."""
    code = ""
    f = HERE / "donate.html"
    if site and f.exists():
        code = re.sub(r"<!--.*?-->", "", f.read_text(encoding="utf-8"), flags=re.S).strip()
    return html.replace("__DONATE_ATTR__", "" if code else "hidden").replace("__DONATE__", code)


def build_viewer(conn, cfg, data_dir, site=False):
    """Write the viewer. Local mode: one self-contained data/viewer.html.
    Site mode: site/index.html plus site/data.json, ready to host."""
    payload = build_payload(conn, cfg, site=site)
    template = (HERE / "viewer_template.html").read_text(encoding="utf-8")
    if site:
        site_dir = Path(cfg["site_dir"])
        if not site_dir.is_absolute():
            site_dir = HERE / site_dir
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        out = site_dir / "index.html"
        out.write_text(_with_donate(template.replace("__DATA__", ""), site=True), encoding="utf-8")
        if cfg.get("custom_domain"):
            (site_dir / "CNAME").write_text(cfg["custom_domain"].strip() + "\n", encoding="utf-8")
    else:
        out = Path(data_dir) / "viewer.html"
        out.write_text(_with_donate(template.replace("__DATA__", _json_for_script(payload)), site=False), encoding="utf-8")
    return out, len(payload["rows"])


# --------------------------------------------------------------------------
# browser session (Playwright)
# --------------------------------------------------------------------------

class BrowserSession:
    def __init__(self, cfg, headed=False):
        self.cfg = cfg
        self.headed = headed or cfg.get("headed", False)

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not self.headed)
        self.ctx = self.browser.new_context(accept_downloads=True, locale="en-US",
                                            timezone_id="America/Chicago")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(60000)
        return self

    def __exit__(self, *exc):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def open_search(self):
        self.page.goto(self.cfg.get("search_url", ERF_SEARCH_URL), wait_until="domcontentloaded")

    def fetch(self, url):
        r = self.ctx.request.get(url, timeout=180000)
        return r.status, r.body(), {k.lower(): v for k, v in r.headers.items()}

    def _guess_input(self, patterns):
        inputs = self.page.query_selector_all("input[type=text], input[type=date], input:not([type])")
        for pat in patterns:
            for inp in inputs:
                ident = ((inp.get_attribute("id") or "") + " " + (inp.get_attribute("name") or "")).lower()
                if re.search(pat, ident):
                    return inp
        return None

    def _find_submit(self):
        sel = self.cfg["selectors"].get("submit")
        if sel:
            return self.page.query_selector(sel)
        best = None
        for b in self.page.query_selector_all("input[type=submit], input[type=button], button"):
            label = ((b.get_attribute("value") or "") + " " + (b.inner_text() or "")).strip().lower()
            if re.search(r"\b(clear|reset|cancel)\b", label):     # "Clear Search Criteria" is not the search button
                continue
            if re.search(r"\bsearch\b", label):
                return b
            if best is None and re.search(r"\bsubmit\b|\bgo\b", label):
                best = b
        return best

    def search(self, start, end):
        """Run the date-range search and return (html, url) of the results page."""
        self.open_search()
        sel = self.cfg["selectors"]
        s_in = self.page.query_selector(sel["start"]) if sel.get("start") else \
            self._guess_input([r"startdate", r"start", r"begin", r"fromdate", r"from"])
        e_in = self.page.query_selector(sel["end"]) if sel.get("end") else \
            self._guess_input([r"enddate", r"end", r"todate", r"_to\b|to$"])
        submit = self._find_submit()
        if not (s_in and e_in and submit):
            raise ProbeNeeded(
                "Could not find the date fields or the search button on the ERF search page. "
                "Run `python psc_watcher.py probe`, then set selectors.start, selectors.end and "
                "selectors.submit in config.json.")
        for box, day in ((s_in, start), (e_in, end)):
            # <input type="date"> only accepts YYYY-MM-DD; a plain text box takes the US format
            box.fill(day.isoformat() if (box.get_attribute("type") or "").lower() == "date"
                     else day.strftime("%m/%d/%Y"))
        submit.click()
        deadline = time.time() + self.cfg.get("results_timeout_seconds", 240)
        while time.time() < deadline:
            for pg in self.ctx.pages:
                try:
                    content = pg.content()
                except Exception:  # page mid-navigation
                    continue
                if _TOTAL_RE.search(BeautifulSoup(content, "html.parser").get_text(" ", strip=True)):
                    return content, pg.url
            time.sleep(1)
        raise RuntimeError("Timed out waiting for ERF results (no 'Total Return' line appeared).")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def collect_range(s, start, end, debug, log=print):
    """Search start..end. ERF only shows part of a big result set, so when fewer rows come back
    than ERF says there are, split the window in half and search each half (down to one day)."""
    html_text, url = s.search(start, end)
    (debug / "last_results.html").write_text(html_text, encoding="utf-8")
    rows, total = parse_results(html_text, url)
    if total is not None and len(rows) < total and (end - start).days > 1:
        mid = start + (end - start) // 2
        log(f"{start} to {end}: ERF reported {total} rows but only {len(rows)} parsed; splitting at {mid}.")
        left, lt = collect_range(s, start, mid, debug, log)
        right, rt = collect_range(s, mid, end, debug, log)
        seen, merged = set(), []
        for r in left + right:
            key = r.get("psc_ref") or id(r)
            if key not in seen:
                seen.add(key)
                merged.append(r)
        return merged, len(merged)
    return rows, total


def cmd_run(args, cfg):
    data_dir = data_path(cfg)
    conn = connect(data_dir)
    today = dt.date.today()
    start = dt.datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else \
        today - dt.timedelta(days=cfg["lookback_days"])
    end = dt.datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else today + dt.timedelta(days=1)
    with BrowserSession(cfg, headed=args.headed) as s:
        debug = data_dir / "debug"
        debug.mkdir(exist_ok=True)
        rows, total = collect_range(s, start, end, debug)
        if total == 0 and not rows:
            # A quiet stretch (weekends, holidays): ERF says "Total Return: 0". That is a valid result.
            print(f"{start} to {end}: ERF reports no filings in this range.")
            new = 0
        else:
            if total is not None and total != len(rows):
                print(f"WARNING: ERF reported {total} rows but {len(rows)} were parsed. "
                      f"Saved page to {debug / 'last_results.html'} for inspection.", file=sys.stderr)
            if not rows:
                raise RuntimeError("No rows parsed from the results page. See data/debug/last_results.html.")
            new = upsert_rows(conn, rows)
            print(f"{start} to {end}: {len(rows)} filings on the page, {new} new.")
        if cfg["download"] and not args.site:
            done, skipped, failed = download_pending(conn, s.fetch, cfg, data_dir)
            print(f"Downloads: {done} saved, {skipped} over the {cfg['max_download_mb']} MB cap, {failed} failed.")
        named = update_dockets(conn, s.fetch, cfg, retry=args.retry_titles)
        missing = conn.execute("SELECT COUNT(*) FROM dockets WHERE title IS NULL OR title=''").fetchone()[0]
        print(f"Case names: {named} looked up this run, {missing} dockets still without a name.")
    client = summaries.make_llm_client(cfg)
    done, ai_used = summarize_dockets(conn, cfg, client=client)
    print(f"Summaries: {done} written ({ai_used} with AI help; AI is {'on' if client else 'off'}).")
    out, n = build_viewer(conn, cfg, data_dir, site=args.site)
    print(f"{'Website' if args.site else 'Viewer'}: {out} ({n} filings)")


def cmd_ingest(args, cfg):
    data_dir = data_path(cfg)
    conn = connect(data_dir)
    html_text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    rows, total = parse_results(html_text, args.base_url)
    if total is not None and total != len(rows):
        print(f"WARNING: page says {total} rows, parsed {len(rows)}.", file=sys.stderr)
    new = upsert_rows(conn, rows)
    print(f"Parsed {len(rows)} filings, {new} new.")
    out, n = build_viewer(conn, cfg, data_dir)
    print(f"Viewer: {out} ({n} filings)")


def cmd_download(args, cfg):
    data_dir = data_path(cfg)
    conn = connect(data_dir)
    with BrowserSession(cfg, headed=args.headed) as s:
        s.open_search()
        done, skipped, failed = download_pending(conn, s.fetch, cfg, data_dir, refs=args.refs)
    print(f"Downloads: {done} saved, {failed} failed.")
    build_viewer(conn, cfg, data_dir)


def cmd_summarize(args, cfg):
    data_dir = data_path(cfg)
    conn = connect(data_dir)
    client = summaries.make_llm_client(cfg)
    done, ai_used = summarize_dockets(conn, cfg, client=client, force=args.force)
    print(f"Summaries: {done} written ({ai_used} with AI help; AI is {'on' if client else 'off'}).")
    out, n = build_viewer(conn, cfg, data_dir, site=args.site)
    print(f"{'Website' if args.site else 'Viewer'}: {out} ({n} filings)")


def cmd_build(args, cfg):
    data_dir = data_path(cfg)
    out, n = build_viewer(connect(data_dir), cfg, data_dir, site=args.site)
    print(f"{'Website' if args.site else 'Viewer'}: {out} ({n} filings)")


def cmd_probe(args, cfg):
    data_dir = data_path(cfg)
    debug = data_dir / "debug"
    debug.mkdir(exist_ok=True)
    if args.docket:
        url = docket_page_url(args.docket)
        if not url:
            raise RuntimeError(f"'{args.docket}' is not a docket number like 6680-UR-124")
        with BrowserSession(cfg, headed=args.headed) as s:
            s.open_search()
            status, body, _ = s.fetch(url)
        text = body.decode("utf-8", errors="replace")
        (debug / f"docket_{args.docket}.html").write_text(text, encoding="utf-8")
        print(f"HTTP {status}, saved to {debug / ('docket_' + args.docket + '.html')}")
        print("Parsed:", json.dumps(parse_docket_page(text), indent=2))
        return
    with BrowserSession(cfg, headed=args.headed) as s:
        s.open_search()
        s.page.wait_for_load_state("networkidle")
        (debug / "search_form.html").write_text(s.page.content(), encoding="utf-8")
        s.page.screenshot(path=str(debug / "search_form.png"), full_page=True)
        lines = []
        for e in s.page.query_selector_all("input, select, textarea, button"):
            lines.append(" | ".join([
                e.evaluate("n => n.tagName.toLowerCase()"),
                "type=" + (e.get_attribute("type") or ""),
                "id=" + (e.get_attribute("id") or ""),
                "name=" + (e.get_attribute("name") or ""),
                "value=" + (e.get_attribute("value") or ""),
            ]))
        (debug / "search_form_fields.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved form details to {debug}. Use the ids to fill selectors in config.json, e.g. \"#ctl00_txtStart\".")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--debug", action="store_true", help="show full error traces")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="search, store, download, rebuild viewer")
    p.add_argument("--since", help="YYYY-MM-DD (default: today minus lookback_days)")
    p.add_argument("--until", help="YYYY-MM-DD (default: tomorrow)")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--site", action="store_true",
                   help="build the public website (site/) instead of the local viewer; no file downloads")
    p.add_argument("--retry-titles", action="store_true", help="retry case-name lookups that failed before")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("ingest", help="parse a saved results page")
    p.add_argument("file")
    p.add_argument("--base-url", default=ERF_SEARCH_URL)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("download", help="download specific filings by PSC Ref")
    p.add_argument("refs", nargs="+", type=int)
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_download)

    p = sub.add_parser("summarize", help="(re)write case summaries from stored case names")
    p.add_argument("--force", action="store_true", help="redo every docket, not just new or changed ones")
    p.add_argument("--site", action="store_true", help="rebuild the public website afterwards")
    p.set_defaults(fn=cmd_summarize)

    p = sub.add_parser("build", help="rebuild the viewer or website from the database")
    p.add_argument("--site", action="store_true", help="write the public website to site/")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("probe", help="dump the search form (or one docket page) for setup")
    p.add_argument("--docket", help="fetch one docket page, e.g. 6680-UR-124, and show what was parsed from it")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_probe)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    try:
        args.fn(args, cfg)
    except ProbeNeeded as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one readable line for scheduled runs
        if args.debug:
            raise
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M} ERROR: {first} (re-run with --debug for the full trace)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
