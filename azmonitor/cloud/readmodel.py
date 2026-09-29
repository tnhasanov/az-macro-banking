"""What the dashboard reads: a derived, disposable projection of the dataset into Postgres.

The dashboard cannot open the dataset. It is a 374 MB SQLite file in object storage, and a web
request that downloaded it would be slow, expensive and — worse — would give the browser a second
copy of the authority. So the worker publishes a projection after every run: the indicators the
dashboard shows, the publications it lists, the editions it offers for download, the job history it
reports on, and the delivery states it surfaces for review.

Three rules keep this honest:

**One authority.** Nothing reads back from Postgres into the engine. The projection is rebuilt from
the dataset on every run and can be dropped and rebuilt at any time without losing anything. A
number that disagrees between the two is a projection bug, never a data question.

**Every figure carries its provenance.** A row without a reporting period, a source and a period
type does not get published, because a number on a dashboard with no date beside it is the failure
this whole system exists to prevent. Observations, forecasts and stress projections are separate
`kind`s and can never be charted as one series by accident.

**The delivery ledger is copied, not moved.** Its authority stays in the engine's own database,
where the send path writes it under a lock. What lands here is a read-only mirror for the screen
that lists deliveries needing a person, and the dashboard cannot resolve one — that is a CLI action
against the real ledger.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..util.log import get_logger

log = get_logger("cloud.readmodel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key           TEXT PRIMARY KEY,
  value         JSONB NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every number the dashboard can show, with the provenance that makes it readable.
CREATE TABLE IF NOT EXISTS indicators (
  series_id     TEXT NOT NULL,
  dims          JSONB NOT NULL DEFAULT '{}'::jsonb,
  period_end    DATE NOT NULL,
  value         DOUBLE PRECISION,
  unit          TEXT,
  label         TEXT,
  -- observation | forecast | stress | policy: never mixed on one axis
  kind          TEXT NOT NULL,
  period_type   TEXT,
  frequency     TEXT,
  scenario      TEXT,
  vintage       TEXT,
  source_id     TEXT,
  dataset_id    TEXT,
  publication_id TEXT,
  published_at  DATE,
  citation      TEXT,
  -- source: published by the CBA or the SSC exactly as shown.
  -- computed: derived here by the metric dictionary from published figures.
  -- A dashboard that blurs the two invites a reader to attribute our arithmetic to the Central Bank.
  origin        TEXT NOT NULL DEFAULT 'source',
  formula       TEXT
);
-- The grain, as a unique index rather than a key, because it involves an expression: a reading is
-- one series, one dimension slice, one period, one kind, and - for anything forward-looking - one
-- scenario and one vintage. Two rows that collide here would be a projection bug, and this is what
-- would catch it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_indicators_grain ON indicators
  (series_id, dims, period_end, kind, COALESCE(scenario, ''), COALESCE(vintage, ''));
CREATE INDEX IF NOT EXISTS ix_indicators_series ON indicators (series_id, period_end DESC);
CREATE INDEX IF NOT EXISTS ix_indicators_kind ON indicators (kind, period_end DESC);

-- What the sources have published, and what this system did about it.
CREATE TABLE IF NOT EXISTS publications (
  publication_id TEXT PRIMARY KEY,
  pub_type      TEXT NOT NULL,
  edition_key   TEXT,
  edition_label TEXT,
  title_en      TEXT,
  reporting_period_start DATE,
  reporting_period_end   DATE,
  published_at  DATE,
  published_at_basis TEXT,
  translation_available_at DATE,
  first_seen_at TIMESTAMPTZ,
  original_language TEXT,
  languages     TEXT[],
  source_urls   JSONB NOT NULL DEFAULT '[]'::jsonb,
  passages      INTEGER NOT NULL DEFAULT 0,
  verified_passages INTEGER NOT NULL DEFAULT 0,
  extraction_status TEXT,
  processed     BOOLEAN NOT NULL DEFAULT false,
  report_generated BOOLEAN NOT NULL DEFAULT false,
  report_edition TEXT
);
CREATE INDEX IF NOT EXISTS ix_publications_released ON publications (published_at DESC);

-- Every report version, and where its files are.
CREATE TABLE IF NOT EXISTS editions (
  report_type   TEXT NOT NULL,
  edition       TEXT NOT NULL,
  version       INTEGER NOT NULL,
  generated_at  TIMESTAMPTZ,
  as_of         DATE,
  status_label  TEXT,
  partial       BOOLEAN NOT NULL DEFAULT false,
  n_slides      INTEGER,
  fingerprint   TEXT,
  trigger       TEXT,
  narrative_mode TEXT,
  numbers_checked INTEGER,
  quality       JSONB NOT NULL DEFAULT '{}'::jsonb,
  reporting_periods JSONB NOT NULL DEFAULT '{}'::jsonb,
  summary       JSONB NOT NULL DEFAULT '[]'::jsonb,
  files         JSONB NOT NULL DEFAULT '[]'::jsonb,
  blob_prefix   TEXT,
  is_latest     BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (report_type, edition, version)
);
CREATE INDEX IF NOT EXISTS ix_editions_latest ON editions (report_type, is_latest, generated_at DESC);

-- Quality checks, so the dashboard can warn without recomputing anything.
CREATE TABLE IF NOT EXISTS quality_checks (
  id            TEXT PRIMARY KEY,
  check_type    TEXT,
  ok            BOOLEAN NOT NULL,
  severity      TEXT,
  failed        INTEGER NOT NULL DEFAULT 0,
  comparisons   INTEGER NOT NULL DEFAULT 0,
  failed_periods TEXT[],
  message       TEXT,
  as_of         DATE
);

-- Scheduled runs: what fired, what it produced, what went wrong.
CREATE TABLE IF NOT EXISTS job_runs (
  run_id        TEXT PRIMARY KEY,
  task          TEXT NOT NULL,
  trigger       TEXT,                       -- vercel_cron | manual | github
  status        TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ,
  local_time    TEXT,
  produced      JSONB NOT NULL DEFAULT '[]'::jsonb,
  delivered     INTEGER NOT NULL DEFAULT 0,
  readiness     JSONB NOT NULL DEFAULT '{}'::jsonb,
  error         TEXT,
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_job_runs_task ON job_runs (task, started_at DESC);

-- A mirror of the delivery ledger. Read-only here: the authority is the engine's own database.
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id   TEXT PRIMARY KEY,
  report_type   TEXT NOT NULL,
  edition       TEXT NOT NULL,
  version       INTEGER,
  channel       TEXT NOT NULL,
  recipient_id  TEXT NOT NULL,
  recipient_hint TEXT,
  status        TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  subject       TEXT,
  sent_at       TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries (status, updated_at DESC);

-- A lease, so two workers cannot process the same dataset at once. See lock.py.
CREATE TABLE IF NOT EXISTS job_locks (
  name          TEXT PRIMARY KEY,
  holder        TEXT NOT NULL,
  acquired_at   TIMESTAMPTZ NOT NULL,
  expires_at    TIMESTAMPTZ NOT NULL,
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Bumped on every acquisition and never decreased. A worker carries the value it was given and
  -- presents it before each persistent write, so one whose lease lapsed mid-run can be told apart
  -- from the one that replaced it. See azmonitor/cloud/lock.py.
  fence         BIGINT NOT NULL DEFAULT 1
);
"""

# Forward-looking series must never be charted as history, so their kind is decided here once.
FORWARD_TYPES = {"forecast": "forecast", "stress_test_projection": "stress"}
POLICY_PREFIXES = ("cba.policy.",)


def dsn() -> str:
    """The connection string, from whichever variable the platform set.

    Vercel's Neon integration sets POSTGRES_URL; Neon's own dashboard gives DATABASE_URL. Accepting
    both means the deployment works whichever way the database was attached.
    """
    for var in ("AZMONITOR_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL", "POSTGRES_PRISMA_URL"):
        value = os.environ.get(var)
        if value:
            return value
    raise RuntimeError(
        "no database is configured: set AZMONITOR_DATABASE_URL, POSTGRES_URL or DATABASE_URL")


def dsn_or_none() -> str | None:
    """The connection string, or None when this deployment has no database.

    A separate function rather than catching the exception at each call site, because "there is no
    database configured" and "the database is unreachable" must never be handled the same way: the
    first is a valid single-host deployment, the second is an outage.
    """
    try:
        return dsn()
    except RuntimeError:
        return None


def connect(url: str | None = None):
    """A connection, with the import kept local so the engine does not need psycopg to run."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("psycopg is not installed; install azmonitor[cloud]") from exc
    return psycopg.connect(url or dsn())


# Columns added after a deployment already exists. Most of this schema is rebuilt from the dataset
# on every run and could simply be dropped, but `job_runs` is not derived from anything - it is the
# only record that a scheduled run happened at all - so the schema is migrated rather than recreated.
MIGRATIONS = (
    "ALTER TABLE indicators ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'source'",
    "ALTER TABLE indicators ADD COLUMN IF NOT EXISTS formula TEXT",
    "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS trigger TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS translation_available_at DATE",
    "ALTER TABLE job_locks ADD COLUMN IF NOT EXISTS fence BIGINT NOT NULL DEFAULT 1",
    # Editions gained provenance of their own once the catalogue moved into object storage.
    "ALTER TABLE editions ADD COLUMN IF NOT EXISTS files_missing JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE editions ADD COLUMN IF NOT EXISTS catalogued_at TIMESTAMPTZ",
    "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT TRUE",
)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        for statement in MIGRATIONS:
            cur.execute(statement)
    conn.commit()


# --------------------------------------------------------------------- publishing

def _kind(period_type: str | None, series_id: str) -> str:
    if period_type in FORWARD_TYPES:
        return FORWARD_TYPES[period_type]
    if series_id.startswith(POLICY_PREFIXES):
        return "policy"
    return "observation"


def _rows_from_observations(db) -> Iterable[tuple]:
    """Published observations, each carrying what a reader needs to interpret it.

    Readings held for review are excluded exactly as they are excluded from every figure in a
    report: the dashboard must not show a number the deck refused to print.
    """
    sql = """
        SELECT o.series_id, o.dims, o.period_end, o.value, o.unit, o.period_type, o.freq,
               o.scenario, o.forecast_vintage, o.source_id, o.dataset_id, o.publication_id,
               o.published_at, o.label_original, r.label_en AS label
        FROM observations o
        LEFT JOIN series_registry r ON r.series_id = o.series_id
        WHERE o.status = 'current'
          AND o.value IS NOT NULL
          AND COALESCE(o.validation_status, 'verified') = 'verified'
    """
    for row in db.conn.execute(sql):
        d = dict(row)
        dims = d.get("dims") or "{}"
        if isinstance(dims, str):
            try:
                dims = json.loads(dims)
            except ValueError:
                dims = {}
        kind = _kind(d.get("period_type"), d["series_id"])
        vintage = d.get("forecast_vintage") or (dims.get("vintage") if isinstance(dims, dict) else None)
        yield (d["series_id"], json.dumps(dims, sort_keys=True), d["period_end"], d["value"],
               d.get("unit"), d.get("label") or d.get("label_original"), kind, d.get("period_type"),
               d.get("freq"), d.get("scenario") or (dims.get("scenario") if isinstance(dims, dict) else None),
               vintage, d.get("source_id"), d.get("dataset_id"), d.get("publication_id"),
               d.get("published_at"), None, "source", None)


def _rows_from_metrics(db) -> Iterable[tuple]:
    """The metric dictionary's computed series.

    A loan-to-deposit ratio is not published by anyone: it is arithmetic over two published figures,
    and the dashboard needs it. It is projected with origin='computed' and its formula attached, so
    a reader can tell our ratio from the Central Bank's own number - which matters most where both
    exist, as they do for the NPL ratio.
    """
    from ..calc.data import ObservationStore
    from ..calc.metrics import MetricEngine

    store = ObservationStore(db)
    eng = MetricEngine(store)
    eng.compute_all()
    for result in eng.results.values():
        series = result.series
        if series is None or series.empty:
            continue
        for period, value in series.items():
            if value is None or (isinstance(value, float) and value != value):
                continue
            yield (result.metric_id, json.dumps(result.dims or {}, sort_keys=True), period,
                   float(value), result.unit, result.label_en, _kind(result.period_type, result.metric_id),
                   result.period_type, None, None, None, None, None, None, None, result.basis,
                   "computed", result.formula)


def publish_indicators(conn, db, *, series_allowlist: set[str] | None = None) -> int:
    """Replace the indicator projection. Whole-table, in one transaction.

    Incremental publishing would be faster and would eventually drift: a series renamed or a reading
    withdrawn upstream would linger. The dataset is the authority and this is cheap to rebuild, so
    it is rebuilt.
    """
    rows = [r for r in _rows_from_observations(db)
            if series_allowlist is None or r[0] in series_allowlist]
    try:
        rows += [r for r in _rows_from_metrics(db)
                 if series_allowlist is None or r[0] in series_allowlist]
    except Exception:
        # A metric that cannot be computed must not cost the dashboard its published figures.
        log.exception("computed metrics could not be projected; published observations were still published")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _indicators (LIKE indicators INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy("COPY _indicators (series_id, dims, period_end, value, unit, label, kind, "
                      "period_type, frequency, scenario, vintage, source_id, dataset_id, "
                      "publication_id, published_at, citation, origin, formula) FROM STDIN") as copy:
            for r in rows:
                copy.write_row(r)
        cur.execute("TRUNCATE indicators")
        cur.execute("INSERT INTO indicators SELECT DISTINCT ON "
                    "(series_id, dims, period_end, kind, COALESCE(scenario,''), COALESCE(vintage,'')) * "
                    "FROM _indicators")
    conn.commit()
    log.info("published %d indicator rows", len(rows))
    return len(rows)


def publish_publications(conn, db) -> int:
    sql = """
        SELECT p.*,
               (SELECT COUNT(*) FROM passages s WHERE s.publication_id = p.publication_id) AS passages,
               (SELECT COUNT(*) FROM passages s WHERE s.publication_id = p.publication_id
                                              AND s.validation_status = 'verified') AS verified
        FROM publications p
    """
    rows = []
    for row in db.conn.execute(sql):
        d = dict(row)
        docs = db.conn.execute(
            "SELECT d.document_url, pd.language FROM publication_documents pd "
            "JOIN documents d ON d.doc_id = pd.doc_id WHERE pd.publication_id = ?",
            (d["publication_id"],)).fetchall()
        languages = sorted({r["language"] for r in docs if r["language"]})
        urls = [{"language": r["language"], "url": r["document_url"]} for r in docs]
        passages, verified = d.get("passages") or 0, d.get("verified") or 0
        status = ("verified" if passages and verified >= passages * 0.5
                  else "partial" if passages else "not extracted")
        rows.append((d["publication_id"], d["pub_type"], d.get("edition_key"), d.get("edition_label"),
                     d.get("title_en"), d.get("reporting_period_start"), d.get("reporting_period_end"),
                     d.get("published_at"), d.get("published_at_basis"), d.get("translation_available_at"),
                     d.get("first_seen_at"), d.get("original_language"), languages, json.dumps(urls),
                     passages, verified, status, bool(passages), False, None))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE publications")
        cur.executemany(
            "INSERT INTO publications (publication_id, pub_type, edition_key, edition_label, title_en, "
            "reporting_period_start, reporting_period_end, published_at, published_at_basis, "
            "translation_available_at, first_seen_at, original_language, languages, source_urls, "
            "passages, verified_passages, extraction_status, processed, report_generated, report_edition) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    conn.commit()
    log.info("published %d publication rows", len(rows))
    return len(rows)


def publish_editions(conn, editions: list[dict[str, Any]]) -> dict[str, Any]:
    """The report archive, as the dashboard lists it.

    Upserted, never truncated. The previous version emptied the table and refilled it from whatever
    editions happened to be on the runner's disk — which, on a fresh runner, is none. One ordinary
    run after a publish would therefore have erased every report the dashboard knew about while the
    files themselves sat safely in the store.

    `editions` now comes from the catalogue in object storage, so it is the whole archive rather
    than one run's output. Even so, nothing is deleted here: a row is only ever added or updated,
    and removing an edition is a deliberate retention action, not a side effect of a run that could
    not see it.
    """
    added = updated = 0
    with conn.cursor() as cur:
        for e in editions:
            row = cur.execute(
                "INSERT INTO editions (report_type, edition, version, generated_at, as_of, status_label, "
                "partial, n_slides, fingerprint, trigger, narrative_mode, numbers_checked, quality, "
                "reporting_periods, summary, files, blob_prefix, is_latest, files_missing, catalogued_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (report_type, edition, version) DO UPDATE SET "
                "  is_latest = EXCLUDED.is_latest, files = EXCLUDED.files, "
                "  blob_prefix = EXCLUDED.blob_prefix, files_missing = EXCLUDED.files_missing "
                "RETURNING (xmax = 0) AS inserted",
                (e["report_type"], e["edition"], e["version"], e.get("generated_at"), e.get("as_of"),
                 e.get("status_label"), bool(e.get("partial")), e.get("n_slides"), e.get("fingerprint"),
                 e.get("trigger"), e.get("narrative_mode"), e.get("numbers_checked"),
                 json.dumps(e.get("quality") or {}), json.dumps(e.get("reporting_periods") or {}),
                 json.dumps(e.get("summary") or []), json.dumps(e.get("files") or []),
                 e.get("blob_prefix"), bool(e.get("is_latest")),
                 json.dumps(e.get("files_missing") or []))).fetchone()
            if row and row[0]:
                added += 1
            else:
                updated += 1

        # `is_latest` is a property of the whole archive, so a version that is no longer newest has
        # to be demoted even though this run never saw it.
        if editions:
            cur.execute(
                "UPDATE editions e SET is_latest = FALSE "
                "WHERE is_latest AND EXISTS (SELECT 1 FROM editions n "
                "  WHERE n.report_type = e.report_type AND n.edition = e.edition "
                "    AND n.version > e.version)")
        total = cur.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
    conn.commit()
    log.info("catalogue: %d added, %d updated, %d rows in total", added, updated, total)
    return {"added": added, "updated": updated, "total": total}


def publish_quality(conn, quality: dict[str, Any]) -> int:
    checks = quality.get("checks") or []
    with conn.cursor() as cur:
        cur.execute("TRUNCATE quality_checks")
        for c in checks:
            cur.execute(
                "INSERT INTO quality_checks (id, check_type, ok, severity, failed, comparisons, "
                "failed_periods, message, as_of) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET ok = EXCLUDED.ok, severity = EXCLUDED.severity",
                (c.get("id") or c.get("check"), c.get("type") or c.get("check", "").split(":")[0],
                 bool(c.get("ok")), c.get("severity"), int(c.get("failed") or 0),
                 int(c.get("n") or 0), [str(p) for p in (c.get("failed_periods") or [])],
                 c.get("message") or c.get("note"), (quality.get("summary") or {}).get("as_of")))
        cur.execute("INSERT INTO meta (key, value, updated_at) VALUES ('quality_summary', %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (json.dumps(quality.get("summary") or {}),))
    conn.commit()
    return len(checks)


def publish_deliveries(conn, ledger_path: Path) -> int:
    """Mirror the delivery ledger. Read-only: the dashboard shows it and cannot change it."""
    if not Path(ledger_path).exists():
        return 0
    from ..delivery.records import DeliveryLedger

    led = DeliveryLedger(ledger_path)
    try:
        rows = [dict(r) for r in led.conn.execute("SELECT * FROM deliveries").fetchall()]
    finally:
        led.close()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE deliveries")
        for r in rows:
            cur.execute(
                "INSERT INTO deliveries (delivery_id, report_type, edition, version, channel, "
                "recipient_id, recipient_hint, status, attempts, subject, sent_at, updated_at, last_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (r["delivery_id"], r["report_type"], r["edition"], r["version"], r["channel"],
                 r["recipient_id"], r["recipient_hint"], r["status"], r["attempts"], r["subject"],
                 r["sent_at"], r["updated_at"], r["last_error"]))
    conn.commit()
    log.info("mirrored %d delivery rows", len(rows))
    return len(rows)


def record_run(conn, run: dict[str, Any]) -> None:
    """One scheduled run, as the monitoring page reports it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, task, trigger, status, started_at, finished_at, local_time, "
            "produced, delivered, readiness, error, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, "
            "finished_at = EXCLUDED.finished_at, produced = EXCLUDED.produced, "
            "delivered = EXCLUDED.delivered, readiness = EXCLUDED.readiness, error = EXCLUDED.error, "
            "detail = EXCLUDED.detail",
            (run["run_id"], run["task"], run.get("trigger"), run["status"], run["started_at"],
             run.get("finished_at"), run.get("local_time"), json.dumps(run.get("produced") or []),
             int(run.get("delivered") or 0), json.dumps(run.get("readiness") or {}),
             run.get("error"), json.dumps(run.get("detail") or {})))
    conn.commit()


def set_meta(conn, key: str, value: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO meta (key, value, updated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (key, json.dumps(value)))
    conn.commit()


def get_meta(conn, key: str) -> Any:
    with conn.cursor() as cur:
        row = cur.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def publish_definitions(conn) -> int:
    """Publish the metric definitions the dashboard is allowed to show.

    The dashboard does not get to choose which figures are headline figures. That choice is already
    made, and validated, in ``config/reports.yaml``: the scorecard the monthly deck renders and the
    sector map the sector review uses. Publishing them here means the screen and the deck answer to
    one definition, so a metric renamed in config cannot leave the dashboard quietly showing the old
    one under the old label.

    Only labels and series ids travel — no values. The numbers still come from ``indicators``, with
    their own periods and provenance attached.
    """
    from .. import config

    reports = config.reports_config()
    scorecard = [
        {"key": row.get("key"), "label": row.get("label"), "series_id": row.get("metric"),
         "dims": row.get("dims") or {}, "change": row.get("change"), "change_kind": row.get("kind"),
         "good": row.get("good", "neutral")}
        for row in (reports.get("monthly", {}).get("scorecard") or [])
        if row.get("metric")
    ]
    sectors = [
        {"key": key, "label": spec.get("label", key),
         "credit_series": spec.get("cba_credit"),
         "activity_series": list(spec.get("ssc_activity") or []),
         "bank_business_series": spec.get("cba_bank_business")}
        for key, spec in (reports.get("sector", {}).get("sectors") or {}).items()
    ]
    # The report names the delivery layer already uses, so a subject line, an archive listing and
    # the dashboard cannot end up calling the same report three different things.
    from ..delivery.dispatch import _report_name

    report_types = ("monthly", "weekly", "sector", "mpr_brief", "fsr_brief", "decision_update")
    set_meta(conn, "definitions", {
        "scorecard": scorecard,
        "sectors": sectors,
        "chart_window_months": (reports.get("rules") or {}).get("chart_window", 36),
        "report_titles": {rt: _report_name(rt) for rt in report_types},
        "status_label": config.settings()["report"].get("status_label"),
    })
    log.info("published %d scorecard rows and %d sectors", len(scorecard), len(sectors))
    return len(scorecard)
