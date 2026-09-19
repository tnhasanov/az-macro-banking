# Operations: refresh, reporting and persistent storage

## 1. Daily refresh cycle

`monitor run-due` (wrapped by `scripts/run_due.sh`) performs, under a job lock:

1. **Discover** current document links on every configured entry page (`config/sources.yaml`). CBA pages list files as `download_item` anchors with a date in the title; SSC monthly editions are one page each with the headline HTML table, the PDF link and the page date.
2. **Compare** with saved state: a file is downloaded and its SHA-256 compared with the stored document for that dataset. Unchanged content is recorded as `unchanged` and not re-parsed. HTML edition pages are fingerprinted on their article content so volatile markup does not trigger reprocessing.
3. **Parse and validate** changed files; run component-sum checks; store observations as a new vintage (new periods and revisions are counted separately). A parser failure marks the document `parse_failed` and leaves prior observations untouched.
4. **Quality checks** (`data/state/quality_report.json`).
5. **Publication policy** (see below) decides whether a monthly edition or weekly digest is due.
6. **State** is written to `data/state/run_due_state.json` and `data/state/last_run_due.json`; structured logs go to `data/logs/monitor-YYYYMM.jsonl`.

Failures are classified in the run summary: `fetch_failed`, `parse_failed`, `parsed_with_check_failures` (data quality), narrative/rendering failure (report status `failed`). A failed fetch is reported as a failure, never as "no new data". Prior successful outputs are never overwritten; a new edition directory is created only on success and `outputs/latest` is updated only afterwards.

### Publication policy (initial)

* **Reporting anchor** = banking month for which both `cba_loans_by_institution` (Table 2.6) and `cba_deposits` (Table 2.11) are parsed and verified.
* **Macro anchors** = SSC headline table (GDP, YTD) and SSC price bulletin (CPI). Their reference periods differ from the banking month and are shown separately on the cover and every slide.
* When a new banking anchor month appears, the run waits up to `schedule.companion_grace_days` (default 7) for the companion tables (bank overview 5.2/5.3/5.4/5.6, rates 3.2.1, currency structure 2.12, sector table 2.8, maturity 2.7, household savings 2.13, CBA analytical balance 2.2). If they arrive, a complete edition is generated; when the grace period ends first, a labelled **partial edition** is generated and a later complete edition supersedes it as a new version.
* If an anchor is missing or invalid, `data/state/monthly_status.json` records `blocked` with the cause and the last successful deck stays in `outputs/latest`.
* The **weekly digest** runs on `schedule.weekly_digest_weekday` (default Tuesday) only if new observations or revisions arrived since the last digest; otherwise `weekly_status.json` records `no_update` and the previous deck remains current.
* An explicit `monitor report ...` call (on demand or for the initial build) generates immediately from the latest validated snapshot, disclosing missing optional blocks and differing reporting periods.
* Re-running `monitor report` with unchanged inputs returns `unchanged` (same fact-pack hash and narrative mode as the last edition) unless `--force` is given.

## 2. Persistent storage

Everything that must survive between runs lives in `AZMONITOR_DATA_DIR` (default `./data`) and `AZMONITOR_OUTPUT_DIR` (default `./outputs`):

```
data/
  monitor.sqlite            documents, observation vintages, dataset_state, discovered_links, vintages, runs, report_editions, series_registry
  raw/<dataset_id>/<sha16>.<ext>   original bytes of every downloaded document
  snapshots/<id>/           observations.parquet, metrics.parquet, documents.parquet, snapshot.json (immutable, referenced by manifests)
  analytics/                rolling observations_current.parquet, metrics_current.parquet
  state/                    last_refresh.json, quality_report.json, monthly_status.json, weekly_status.json, run_due_state.json, last_run_due.json, fact_packs/, run_due.lock
  logs/                     monitor-YYYYMM.jsonl, run_due_YYYYMMDD.log
outputs/
  monthly/<edition>/v<N>_<ts>/   deck, pdf, workbook, fact_pack.json, narrative.json, manifest.json, quality_report.json, previews/
  weekly/<date>/v<N>_<ts>/
  sector/<sector>_<date>/v<N>_<ts>/
  latest/monthly -> ...          pointer updated only after a successful validated run
```

A fresh clone has no reporting memory. A cloud or container runner must restore `data/` (and ideally `outputs/`) before the check and save them afterwards. `scripts/run_due.sh` supports this through `AZMONITOR_RESTORE_CMD` / `AZMONITOR_SAVE_CMD`, for example:

```bash
AZMONITOR_RESTORE_CMD='aws s3 sync s3://my-bucket/azmonitor/data "$AZMONITOR_DATA_DIR" --delete'
AZMONITOR_SAVE_CMD='aws s3 sync "$AZMONITOR_DATA_DIR" s3://my-bucket/azmonitor/data && aws s3 sync "$AZMONITOR_OUTPUT_DIR" s3://my-bucket/azmonitor/outputs'
```

Any equivalent (rclone, a mounted volume, `git lfs` for outputs) works. SQLite in WAL mode is safe for a single writer; the job lock prevents concurrent runs. The design ports to a managed database and object storage later without changing the parsers or calculations.

### Vintages and the as-of definition

* Observations are append-only. A replacement file with a different value for an existing (series, dims, period) supersedes the old row (`status='superseded'`, `superseded_at`) and adds the new one with a new `vintage_id`. Revisions since the previous edition appear in appendix A03 and the workbook `Revisions` sheet.
* `--as-of` is an information cutoff at 23:59 Asia/Baku. Reports are built in *reconstructed* mode (current vintages, cutoff applied to reference periods). A true historical information set (`first_observed_at <= cutoff`) is only genuine for cutoffs after the dataset started recording vintages, and is labelled `historical_information_set` when used.

## 3. Scheduled execution (chosen path: OS scheduler on a persistent host)

The scheduler-neutral command is `scripts/run_due.sh`. Configured default: daily at 09:00 Asia/Baku.

**cron** (host with the repository, Python, LibreOffice and poppler installed via `scripts/setup_env.sh`):

```cron
CRON_TZ=Asia/Baku
0 9 * * * cd /opt/az-macro-banking && ./scripts/run_due.sh >> data/logs/cron.log 2>&1
```

**systemd timer** (alternative):

```ini
# /etc/systemd/system/azmonitor.service
[Service]
Type=oneshot
WorkingDirectory=/opt/az-macro-banking
Environment=TZ=Asia/Baku
ExecStart=/opt/az-macro-banking/scripts/run_due.sh
# /etc/systemd/system/azmonitor.timer
[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Baku
Persistent=true
[Install]
WantedBy=timers.target
```

**Hosted runner / Claude Code cloud routine** (optional wrapper): schedule a routine that runs `bash scripts/setup_env.sh --quiet && ./scripts/run_due.sh` with `AZMONITOR_RESTORE_CMD`/`AZMONITOR_SAVE_CMD` pointing at object storage, and allow outbound access to `www.cbar.az`, `uploads.cbar.az` and `www.stat.gov.az`. LibreOffice must be installable in the runner image for PDF output; otherwise the run still produces PPTX/XLSX and marks PDF as skipped. The `.claude/hooks/session-start.sh` hook installs the environment in Claude Code on the web sessions.

**Deployment status:** the local pipeline and this scheduling path are implemented and tested (`run-due --dry-run` and a real run in the build environment). No persistent runner or schedule has been configured in the user's environment yet, so unattended automation is not active. External delivery (messaging, e-mail, webhook) is disabled until a destination is configured and enabled in `settings.yaml`.

Runtime limits: `settings.schedule.max_runtime_minutes` is a documented budget for the scheduler timeout; HTTP retries, backoff and size caps are in `settings.http`. Exit codes of `run_due.sh`: 0 ok, 2 partial, 3 failed, 4 skipped (lock held).

## 4. Reporting

* Monthly: `monitor report --type monthly [--as-of D] [--facts-only | --narrative-file F] [--lang en|az] [--force]`.
* Weekly: `monitor report --type weekly --since D`.
* Sector: `monitor report --type sector --sector agriculture|construction|trade|transport|industry`.
* Narrative for a development or cloud-routine run: build the fact pack (`monitor fact-pack`), write `narrative.json` following `azmonitor/narrative/contract.py` (Claude Code can author it from `data/state/fact_packs/*.json`), then render with `--narrative-file`. The validator rejects unsupported numbers, unknown metric refs, slide ids or malformed periods and falls back to facts-only text for the affected items; results are recorded in `narrative.json` and `manifest.json`.
* API narrative: set `narrative.provider: api`, export `ANTHROPIC_API_KEY` and `AZMONITOR_NARRATIVE_MODEL`, and install `pip install -e .[api]`. Usage is measured and written to the manifest.

## 5. Adding a source or metric

1. Add the dataset under its source in `config/sources.yaml` (title or href pattern, parser id, parser spec, role, expected lag, checks). Parser ids are listed in `azmonitor/parsers/registry.py`; a new layout needs a parser function that returns `Observation` records with cell references.
2. Run `monitor refresh --dataset <id>`; inspect `monitor status` and `data/state/quality_report.json`.
3. Add metrics to `config/metrics.yaml` (formula, inputs, unit, dims, allowed period types). Incompatible period types are rejected at run time and reported in the quality report.
4. Wire the metric into a slide via `azmonitor/facts.py` (fact pack) and, if a new visual is needed, `azmonitor/render/monthly.py`; scorecard rows only need `config/reports.yaml`.
5. Add a fixture-based test under `tests/`.

## 6. Security notes

Secrets come only from the environment or `.env` (git-ignored). Downloaded workbooks are read with data-only loaders (no macro execution). Source text is treated as data; narrative generation receives structured facts, not raw documents. Logs contain URLs and hashes, never credentials.
