# Azerbaijan Macro & Banking Monitor

Collects official Azerbaijani statistics (Central Bank of Azerbaijan, State Statistical Committee), keeps an append-only historical dataset with document provenance, calculates comparable indicators with deterministic code, and produces recurring editable PowerPoint decks with PDF and Excel outputs for a bank CRO / Management Board audience.

Everything numeric is computed by code from stored, unrounded observations. Narrative text is generated separately (facts-only fallback, an analyst-authored JSON file, or an optional API provider) and every number in it is validated against the fact pack before rendering.

## What is in the box

| Area | Implementation |
|---|---|
| Source discovery | `azmonitor/discovery` – resolves current file links from the CBA pages (`a.download_item` anchors) and walks the SSC monthly-edition pages |
| Collection | `azmonitor/ingest/fetch.py` – bounded retries, request spacing, size caps, raw bytes + SHA-256 stored under `data/raw/<dataset>/` |
| Parsers | `azmonitor/parsers` – CBA year/month matrices, rate blocks, bank-overview tables, regional snapshots; SSC HTML headline table (current and legacy layouts), SSC monthly PDF report (text layer), SSC open-data workbooks |
| Storage | SQLite (`data/monitor.sqlite`): documents, append-only observation vintages, dataset state, runs, editions; Parquet snapshots per report in `data/snapshots/` |
| Calculations | `config/metrics.yaml` + `azmonitor/calc/metrics.py` – growth, shares, contributions, spreads, ratio decompositions, YTD→monthly with January reset, annualised ROA/ROE, credit-to-GDP |
| Fact pack | `azmonitor/facts.py` – validated values, comparables, chart data, evidence document ids, availability matrix, monitoring flags, quality checks |
| Narrative | `azmonitor/narrative` – facts-only generator, JSON contract, grounding validator, optional Anthropic API provider (disabled without credentials) |
| Rendering | `azmonitor/render` – python-pptx deck in the supplied ATB design (native charts/tables, speaker notes), LibreOffice PDF, PNG previews, openpyxl workbook |
| Reports | monthly monitor (18 slides + appendices), weekly release digest, sector review |
| Automation | `monitor run-due` (locked, idempotent) via `scripts/run_due.sh`; see `docs/operations.md` |

## Requirements

* Python 3.11+
* LibreOffice Impress (`soffice`) for PDF rendering, `poppler-utils` (`pdftoppm`) for previews
* Fonts: the deck uses Segoe UI (as in the reference design); on Linux `scripts/setup_env.sh` installs Open Sans and a fontconfig alias so PDFs render with similar metrics
* Network access to `www.cbar.az`, `uploads.cbar.az`, `www.stat.gov.az`

## Install

```bash
git clone <this repository> && cd az-macro-banking
bash scripts/setup_env.sh          # apt packages (LibreOffice, poppler, fonts) + pip install -e .[dev]
cp .env.example .env               # optional: persistent paths, API key for narrative generation
```

Without root access run `bash scripts/setup_env.sh --no-apt` and install LibreOffice/poppler separately; the pipeline and the PPTX/XLSX outputs work without them (PDF and previews are then reported as skipped, never claimed).

## Quick start (one-command refresh and report)

```bash
python -m azmonitor.cli backfill --start 2020-01     # first run: discover, download and parse all available history
python -m azmonitor.cli validate                     # data-quality checks -> data/state/quality_report.json
python -m azmonitor.cli report --type monthly --facts-only            # facts-only deck, PDF, workbook
python -m azmonitor.cli report --type monthly --narrative-file narratives/monthly_2026-07_analyst.json
```

`monitor` is also installed as a console script (`monitor refresh`, `monitor report ...`).

Outputs land in `outputs/monthly/<edition YYYY-MM>/v<N>_<timestamp>/` with:

```
AZ_Macro_Banking_Monitor_<edition>_v<N>_<ts>.pptx   editable deck (native charts and tables, speaker notes with sources)
AZ_Macro_Banking_Monitor_<edition>_v<N>_<ts>.pdf    rendered from the same deck (LibreOffice)
AZ_Macro_Banking_Monitor_<edition>_v<N>_<ts>.xlsx   index, definitions, source register, documents, observations, metrics, chart data, KPIs, revisions, quality, availability, narrative
fact_pack.json        every value shown, with comparables, periods, evidence ids and approved numbers
narrative.json        findings / slide texts / questions with classification and validation results
manifest.json         reporting periods, anchors, snapshot id, config hashes, file list, narrative validation, API usage
quality_report.json   data-quality checks
previews/slide-NN.png slide images for inspection
```

`outputs/latest/monthly` points to the last successful validated edition (symlink, or copy on file systems without symlinks) and `outputs/latest/monthly.json` records it. Prior editions and versions are preserved.

## Commands

| Command | Behaviour |
|---|---|
| `monitor discover [--source ID] [--all]` | Resolve current document links from the entry pages and record them (no downloads) |
| `monitor backfill --start YYYY-MM` | Download and parse all available history from the start month (CBA files contain their full history; SSC edition pages are walked back to the start year) |
| `monitor refresh [--source ID] [--dataset ID]` | Daily check: discover, download changed files (by content hash), parse, store new vintages; unchanged files are recognised and skipped |
| `monitor reparse [--dataset ID]` | Re-run parsers on stored documents after a parser change (new vintages only where values change) |
| `monitor validate` | Component-sum, reconciliation, decomposition, identity, label and freshness checks |
| `monitor status` | Dataset state and recent editions |
| `monitor fact-pack --report monthly --as-of YYYY-MM-DD` | Build the structured fact pack only |
| `monitor report --type monthly --as-of YYYY-MM-DD [--facts-only \| --narrative-file F] [--lang en\|az] [--force]` | Monthly edition (blocked status if anchors are missing; `unchanged` if inputs are identical to the last edition) |
| `monitor report --type weekly --since YYYY-MM-DD` | Weekly digest of documents/observations first seen since the date (`no_update` status if nothing new) |
| `monitor report --type sector --sector agriculture` | 8-slide sector review (`agriculture`, `construction`, `trade`, `transport`, `industry`) |
| `monitor run-due [--dry-run]` | Scheduler entry point: refresh, validate, apply the monthly/weekly publication policy, write state |

`--as-of` is an information cutoff at 23:59 Asia/Baku on the given date. The current build reports in *reconstructed* mode (current vintages); a genuine historical information set is available (`historical=True` in `FactPackBuilder`) only for periods after the dataset started recording vintages, and is labelled as such in the fact pack (`information_set_mode`).

## Configuration (no code changes needed)

| File | Purpose |
|---|---|
| `config/settings.yaml` | timezone, language, history start, chart window, paths, schedule (09:00 Baku, 7-day companion grace), narrative provider, delivery (disabled), materiality by series type, monitoring flags |
| `config/sources.yaml` | source register: entry pages, datasets, title patterns, parser specs (sheet, columns, row regexes), expected lags, roles (anchor / companion / optional / reference), component-sum checks, CBA→SSC sector mapping |
| `config/metrics.yaml` | metric dictionary: formula, inputs, units, dimension slices, allowed period types |
| `config/reports.yaml` | slide blueprint and ids, critical anchors and companions, scorecard rows, weekly/sector definitions |
| `config/theme.yaml` | colours, fonts, sizes, layout grid, logo path (taken from the supplied ATB reference deck; replace with an approved asset or set `logo: null`) |
| `config/glossary.yaml` | controlled terminology in `en` and `az` |
| `.env` (from `.env.example`) | `AZMONITOR_DATA_DIR`, `AZMONITOR_OUTPUT_DIR`, `ANTHROPIC_API_KEY`, `AZMONITOR_NARRATIVE_MODEL`, delivery URL |

* **Language**: set `language: az` in `settings.yaml` or pass `--lang az`. The same calculations, fact pack and sources are used; labels come from `glossary.yaml` (extend the `az` entries for full coverage; missing terms fall back to English).
* **Template**: edit `config/theme.yaml`; the renderer reads colours, fonts, sizes, layout margins and the logo from it. Slide layouts are the reusable helpers in `azmonitor/render/builder.py`.
* **Metrics / slides**: add entries to `metrics.yaml`; add or reorder slide ids and scorecard rows in `reports.yaml`.
* **Schedule**: `settings.schedule` (check time is documented for the OS scheduler; grace period and weekly weekday are applied by `run-due`).

## Narrative modes

1. **Facts-only** (default, no API): complete descriptive text generated from the fact pack; every slide labelled as such.
2. **Analyst file** (`--narrative-file`): JSON following `azmonitor/narrative/contract.py` (findings with classification, slide titles/interpretations/so-what/caveats, management questions). `narratives/monthly_2026-07_analyst.json` is the narrative prepared for the first report. The validator checks every number against the fact pack's approved values, every metric ref, slide id and period; rejected items fall back to facts-only text and the validation result is stored in `narrative.json` and the manifest.
3. **API provider** (`narrative.provider: api` in settings, `ANTHROPIC_API_KEY` and `AZMONITOR_NARRATIVE_MODEL` set, `pip install -e .[api]`): the fact pack is sent with the contract; the response is validated the same way. No paid call is required for collection, testing or facts-only reports, and no model id is hardcoded. Measured usage (input/output tokens) is written to the manifest when a call is made.

## Data model (short)

Each observation stores series id, dimensions, period start/end, frequency, period type (month-end stock, monthly flow, YTD flow, YTD average, index, average rate), unit/scale/currency, institutional population, source and document id, sheet/cell reference, original label, extraction method, vintage id, first-observed time, status (`current` / `superseded`), preliminary/marker flags and the explicit publication date (or `NULL` with the basis recorded). Replacement files create new vintages; prior values remain stored and are listed in the workbook `Revisions` sheet and appendix A03.

## Tests

```bash
python -m pytest -q
```

Focused tests on real extraction fixtures (CBA workbooks, SSC HTML pages in both layouts, SSC report text), number/period normalisation, January reset, contribution and decomposition reconciliation, incompatible period types, vintage/revision behaviour, idempotent re-runs, parser failures not touching stored data, and narrative grounding.

## Status and limitations of this build

* Verified working end-to-end on real data: CBA monetary tables (24 datasets), CBA bank overview (balance sheet, P&L, portfolio, NPL, sectors, participants), SSC headline table (monthly editions back to 2020 via the news pages), SSC monthly report PDFs (last 41 editions), SSC price bulletin and open-data workbooks.
* Not published in the collected tables and therefore not shown: regulatory capital adequacy, LCR/NSFR, balance of payments (90-day lag, not yet collected), CBA FX interventions, IFRS stage aggregates, official real-wage index, sector producer prices.
* Unattended automation is **not** active until a scheduler is configured on a persistent runner (see `docs/operations.md`). External delivery is disabled.
* Rendering fonts: the `.pptx` uses Segoe UI; Linux PDF rendering substitutes Open Sans (metrics differ slightly).
