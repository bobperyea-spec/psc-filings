# PSC Wisconsin filings watcher

Collects filings posted to the Public Service Commission of Wisconsin's Electronic Records
Filing (ERF) system and shows them in a searchable table with:

- the docket number, with the case code highlighted (the letters, such as UR or GP) and its plain-English meaning
- the case name for each docket, taken from the PSC's docket page
- a short summary of each case: the project type and the counties its title names, for example
  "Gas power generation - Milwaukee" or "Transmission line - Ashland, Iron"
- a key explaining the case codes (the official PSC list, 184 codes)
- filters for case type, county, document type, docket, date and a watchlist

It runs two ways: as a local viewer on your own computer, or as a public website that updates itself.

## Part 1: Put it online (GitHub Pages, free, updates hourly)

1. Create a free GitHub account if you don't have one, then create a new public repository
   (for example `psc-filings`).
2. Upload everything in this folder to the repository, including the hidden `.github` folder.
3. In the repository go to Settings, then Pages, and set Source to "GitHub Actions".
4. Go to the Actions tab and run "Update PSC filings site" by hand (Run workflow). The first run takes
   a few minutes. To start with more history, type a start date such as 2026-10-01 in the box.
5. When it finishes, your site address appears in the run summary and under Settings, then Pages.
6. After that it runs by itself every hour. GitHub emails you if a run fails.

To use your own address (for example psc.bobperyea.com): put it in `custom_domain` in `config.json`,
then in Settings, then Pages, enter the same name and add the DNS record GitHub shows you.

What the public site shows: filing details and links to each filing on the PSC's own site. It does not
copy the documents themselves. Confidential and in-camera filings (the padlock rows) are listed but
never linked. Names of individuals who submit public comments are replaced with
"Public comment (name withheld)"; set `mask_public_comments` to false in `config.json` to show them.

The repository is public, and so is the database file in it. Everything in it is already public on the
PSC's site.

If the first run fails with a connection or "access denied" error, the PSC's server may be refusing
GitHub's machines. The same code runs on any computer or small server that can reach the PSC: run
`python psc_watcher.py run --site` on a schedule and upload the `site` folder to any web host.

## Part 2: Run it on your own computer

1. Install Python 3.10 or newer.
2. In this folder run: `pip install -r requirements.txt`
3. Run: `playwright install chromium`
4. Run the self-check: `python -m unittest discover -s tests`
5. First real run: `python psc_watcher.py run --headed` (the browser window shows what it does)
6. Open `data/viewer.html` in your browser.

Locally it also saves the documents themselves (files under the size cap) next to the database.

## Commands

- `run`: searches ERF for the last `lookback_days` days across all dockets, saves new filings, looks up
  case names for new dockets, downloads files under the size cap, rebuilds the viewer.
  `--site` builds the public website in `site/` instead (no file downloads).
  `--since 2026-10-01` backfills. `--retry-titles` retries case names that failed.
- `download 616179 616180`: fetch specific filings by PSC Ref, ignoring the size cap.
- `build` (or `build --site`): rebuild the viewer or website from the database.
- `summarize`: write summaries for cases that don't have a current one (`--force` redoes all).
- `ingest saved_page.html`: read a results page you saved from your browser. No network needed.
- `probe`: save the search form's field names and a screenshot to `data/debug/`.
- `probe --docket 6680-UR-124`: fetch one docket page and print what was read from it (case name,
  status, applicant, coordinator).

## If something doesn't work on the first run

- The date fields and GO button: these were checked against the live ERF form on Oct 25, 2026 (date boxes
  `#datepicker_start` and `#datepicker_end`, GO button `#btn_search`) and are filled in by default. If the PSC
  redesigns the form, run `probe`, open `data/debug/search_form_fields.txt`, and put the new ids in
  `config.json` under `selectors`.
- Case names stay blank: run `probe --docket 6680-UR-124` and send me the saved page in `data/debug/`.
  The reader takes the title and status from the docket page's heading and falls back to Title, Status,
  Applicant and Case Coordinator labels. Dockets without a name show "(case name not loaded yet)" and are retried.
- A day with no filings (weekends, holidays) is normal: ERF answers "Total Return: 0" and the run finishes
  without changing anything.
- Row count warning: if the results page says "Total Return: 822 rows" and the script parsed a different
  number, it prints a warning and saves the page to `data/debug/last_results.html`.

## Case summaries (project type and counties)

Each case gets a two-to-four word project type and a list of counties, shown in bold above the case name.

- Project type comes from the case title using fixed rules: gas power generation, solar generation, wind
  generation, battery storage, transmission line, LNG facility, gas pipeline, water system, electric and gas
  rates, and so on. If the title doesn't match a rule, the meaning of the case code is used instead.
- Counties are listed only when the case title names them ("Rock and Walworth Counties", "Dane County",
  "Ozaukee Co."). Towns, cities and villages are not turned into counties, because many town names repeat
  across counties, and counties in other states (such as Dodge County, Minnesota) are ignored. Construction
  cases whose title names no county show "(county not stated in title)" so a blank isn't mistaken for
  "statewide".
- The summary is built from the case title, not from the filings themselves. A case whose location only
  appears inside the application document will show no county until a rule for reading those documents is added.

Optional AI help: if a case's title doesn't match any rule, or mentions a county in wording the rules can't
read, the script can ask Claude to write the label. This is off unless you provide a key, and it only runs
once per case. Whatever the AI returns is checked against the case title: a county that isn't literally in the
title is thrown away, and a rules-based project type is never replaced.

To turn it on for the website:
1. Get an API key from the Anthropic Console (console.anthropic.com).
2. In your GitHub repository go to Settings, then Secrets and variables, then Actions.
3. Choose New repository secret, name it `ANTHROPIC_API_KEY`, paste the key, and save.
4. Run the workflow by hand once. The run log shows how many summaries used AI help.

To turn it on for a local run, set the `ANTHROPIC_API_KEY` environment variable before `run`.
`max_llm_per_run` (default 60) caps the calls per run, and `llm_summaries: false` switches it off entirely.
After changing rules or turning AI on, `python psc_watcher.py summarize --force` redoes every case.

## Case codes

A docket number has three parts: utility ID, case code, case number. In `6680-UR-124`, 6680 is the
utility (Wisconsin Power and Light), UR is the case code (combined utility rates), 124 is the case
number. Most codes are two letters; a few are one or three (CPF, PGA). The table in `case_types.json` is
a copy of the PSC's Case Type Lookup page (https://apps.psc.wi.gov/ERF/ERFsearch/content/caseTypeLookup.aspx)
as of October 2026. A code the PSC adds later shows as "Code not in the PSC list" until the file is updated.

## Settings (config.json)

- `max_download_mb`: local runs skip files larger than this (default 5). One docket can post hundreds of
  15 MB exhibit parts in an afternoon.
- `watch_terms`: words or docket prefixes that highlight a row. Starter list: Beloit, Rock County, 6680-.
- `viewer_days`: how many days the page loads (everything stays in the database).
- `lookback_days`: how many days each run searches (default 2).
- `max_docket_fetches`: case-name lookups per run (default 80).
- `llm_summaries`, `llm_model`, `max_llm_per_run`: the optional AI step described above.
- `delay_seconds`: pause between requests, to go easy on the PSC's server.
- `custom_domain`, `mask_public_comments`: see Part 1.

Received times are Central time, as shown on ERF.
