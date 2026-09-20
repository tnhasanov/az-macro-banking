/**
 * Reading the projection the worker publishes.
 *
 * Everything here is read-only. The dashboard has no write path into the data at all: the engine
 * owns the dataset, the worker owns the projection, and a web request that could change either
 * would give the system a second authority for its numbers. The one exception is the delivery
 * mirror, which the dashboard displays and still cannot act on — resolving an uncertain delivery is
 * a deliberate command-line action against the real ledger, not a button.
 *
 * Every query returns provenance alongside the value. A figure without its reporting period and
 * source is exactly the thing this whole system exists to prevent, so the types make it awkward to
 * read a number without one.
 */
import postgres from "postgres";

let client: ReturnType<typeof postgres> | null = null;

/** Lazy, so a build without a database configured still succeeds and fails at request time. */
export function sql() {
  if (!client) {
    const url =
      process.env.AZMONITOR_DATABASE_URL ??
      process.env.POSTGRES_URL ??
      process.env.DATABASE_URL;
    if (!url) throw new Error("No database is configured: set AZMONITOR_DATABASE_URL");
    client = postgres(url, {
      // Serverless: many short-lived invocations, so keep the pool tiny and let it idle away.
      max: 3,
      idle_timeout: 20,
      connect_timeout: 10,
      prepare: false, // pgbouncer-compatible, which Neon's pooled endpoint requires
      types: {
        // A reporting period is a calendar date, not an instant. Left to its default the driver
        // turns `2026-07-31` into a Date at midnight in the *server's* zone, which in any zone west
        // of UTC formats back as the 30th — a whole month wrong once it is labelled "July".
        // Keeping the column as the string Postgres sent removes the opportunity entirely.
        date: {
          to: 1082,
          from: [1082],
          serialize: (x: string) => x,
          parse: (x: string) => x,
        },
      },
    });
  }
  return client;
}

export type Kind = "observation" | "forecast" | "stress" | "policy";
export type Origin = "source" | "computed";

export interface Point {
  period_end: string;
  value: number;
}

export interface SeriesMeta {
  series_id: string;
  label: string | null;
  unit: string | null;
  kind: Kind;
  origin: Origin;
  period_type: string | null;
  formula: string | null;
  source_id: string | null;
  latest_period: string | null;
  published_at: string | null;
}

export interface Series extends SeriesMeta {
  points: Point[];
  scenario?: string | null;
  vintage?: string | null;
}

/** The newest reading of a series, with everything needed to caption it honestly. */
export async function latest(seriesId: string): Promise<(SeriesMeta & Point) | null> {
  const rows = await sql()<(SeriesMeta & Point)[]>`
    SELECT series_id, label, unit, kind, origin, period_type, formula, source_id,
           period_end, value, published_at, period_end AS latest_period
    FROM indicators
    WHERE series_id = ${seriesId} AND kind IN ('observation', 'policy')
    ORDER BY period_end DESC
    LIMIT 1`;
  return rows[0] ?? null;
}

export async function latestMany(ids: string[]): Promise<Record<string, SeriesMeta & Point>> {
  if (ids.length === 0) return {};
  const rows = await sql()<(SeriesMeta & Point)[]>`
    SELECT DISTINCT ON (series_id)
           series_id, label, unit, kind, origin, period_type, formula, source_id,
           period_end, value, published_at, period_end AS latest_period
    FROM indicators
    WHERE series_id = ANY(${ids}) AND kind IN ('observation', 'policy')
    ORDER BY series_id, period_end DESC`;
  return Object.fromEntries(rows.map((r) => [r.series_id, r]));
}

/**
 * One series as a time series.
 *
 * `kind` is required rather than defaulted, because the whole point of storing it is that an
 * observation and a projection must never end up on the same axis by accident.
 */
export async function series(
  seriesId: string,
  kind: Kind = "observation",
  months = 60,
  dims?: Record<string, string>,
): Promise<Series | null> {
  // `dims` selects one slice of a dimensioned series — the AZN leg of a rate published for both
  // currencies, say. Without it, a dimensioned series would return every slice interleaved on one
  // axis, which is exactly the kind of silent mixing this system is built to prevent.
  const client = sql();
  const rows = await client<
    (Point & SeriesMeta & { scenario: string | null; vintage: string | null })[]
  >`
    SELECT period_end, value, series_id, label, unit, kind, origin, period_type, formula,
           source_id, published_at, scenario, vintage, period_end AS latest_period
    FROM indicators
    WHERE series_id = ${seriesId} AND kind = ${kind}
      ${dims ? client`AND dims @> ${client.json(dims)}::jsonb` : client``}
    ORDER BY period_end DESC
    LIMIT ${months}`;
  if (rows.length === 0) return null;
  const head = rows[0];
  return {
    series_id: head.series_id,
    label: head.label,
    unit: head.unit,
    kind: head.kind,
    origin: head.origin,
    period_type: head.period_type,
    formula: head.formula,
    source_id: head.source_id,
    published_at: head.published_at,
    latest_period: head.period_end,
    scenario: head.scenario,
    vintage: head.vintage,
    points: rows.map((r) => ({ period_end: r.period_end, value: r.value })).reverse(),
  };
}

/** Several series for one chart. Missing ones are dropped rather than faked. */
export async function seriesMany(ids: string[], kind: Kind = "observation", months = 60) {
  const out = await Promise.all(ids.map((id) => series(id, kind, months)));
  return out.filter((s): s is Series => s !== null);
}

/** The latest reading of each series, keyed by id, for a ranked comparison across categories. */
export async function latestForCategories(
  entries: { key: string; label: string; series_id: string }[],
): Promise<{ key: string; label: string; value: number; meta: SeriesMeta & Point }[]> {
  if (entries.length === 0) return [];
  const found = await latestMany(entries.map((e) => e.series_id));
  return entries
    .map((e) => {
      const meta = found[e.series_id];
      return meta ? { key: e.key, label: e.label, value: meta.value, meta } : null;
    })
    .filter((r): r is { key: string; label: string; value: number; meta: SeriesMeta & Point } => r !== null);
}

/** Forecast rounds, each kept whole: one vintage is one line, never blended with another. */
export async function forecastVintages(seriesId: string) {
  const rows = await sql()<
    { vintage: string; scenario: string; period_end: string; value: number; unit: string | null }[]
  >`
    SELECT vintage, scenario, period_end, value, unit
    FROM indicators
    WHERE series_id = ${seriesId} AND kind = 'forecast'
    ORDER BY vintage DESC, period_end`;
  const byVintage = new Map<string, typeof rows>();
  for (const r of rows) {
    const key = `${r.vintage ?? "unknown"}`;
    if (!byVintage.has(key)) byVintage.set(key, [] as never);
    byVintage.get(key)!.push(r);
  }
  return [...byVintage.entries()].map(([vintage, points]) => ({ vintage, points }));
}

/** Stress paths, grouped by the exercise that produced them. Never a continuation of history. */
export async function stressPaths(seriesId: string) {
  return sql()<
    { scenario: string; period_end: string; value: number; unit: string | null }[]
  >`
    SELECT scenario, period_end, value, unit
    FROM indicators
    WHERE series_id = ${seriesId} AND kind = 'stress'
    ORDER BY scenario, period_end`;
}

export interface Edition {
  report_type: string;
  edition: string;
  version: number;
  generated_at: string | null;
  as_of: string | null;
  status_label: string | null;
  partial: boolean;
  n_slides: number | null;
  fingerprint: string | null;
  trigger: string | null;
  narrative_mode: string | null;
  numbers_checked: number | null;
  quality: Record<string, number>;
  reporting_periods: Record<string, string>;
  summary: string[];
  files: { name: string; bytes: number; key: string }[];
  blob_prefix: string | null;
  is_latest: boolean;
}

export async function editions(reportType?: string, limit = 200): Promise<Edition[]> {
  return reportType
    ? sql()<Edition[]>`SELECT * FROM editions WHERE report_type = ${reportType}
                       ORDER BY generated_at DESC NULLS LAST LIMIT ${limit}`
    : sql()<Edition[]>`SELECT * FROM editions ORDER BY generated_at DESC NULLS LAST LIMIT ${limit}`;
}

export async function edition(reportType: string, ed: string, version: number) {
  const rows = await sql()<Edition[]>`
    SELECT * FROM editions
    WHERE report_type = ${reportType} AND edition = ${ed} AND version = ${version}`;
  return rows[0] ?? null;
}

export async function latestEditions(): Promise<Edition[]> {
  return sql()<Edition[]>`
    SELECT DISTINCT ON (report_type) * FROM editions
    ORDER BY report_type, generated_at DESC NULLS LAST`;
}

export interface Publication {
  publication_id: string;
  pub_type: string;
  edition_label: string | null;
  title_en: string | null;
  reporting_period_end: string | null;
  published_at: string | null;
  published_at_basis: string | null;
  translation_available_at: string | null;
  first_seen_at: string | null;
  original_language: string | null;
  languages: string[];
  source_urls: { language: string; url: string }[];
  passages: number;
  verified_passages: number;
  extraction_status: string | null;
  processed: boolean;
  report_generated: boolean;
}

export async function publications(limit = 60): Promise<Publication[]> {
  return sql()<Publication[]>`
    SELECT * FROM publications ORDER BY published_at DESC NULLS LAST LIMIT ${limit}`;
}

export interface JobRun {
  run_id: string;
  task: string;
  trigger: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  local_time: string | null;
  produced: { report_type: string; status: string }[];
  delivered: number;
  readiness: Record<string, { state: string; ok: boolean; note?: string }>;
  error: string | null;
  detail: Record<string, unknown>;
}

export async function jobRuns(limit = 40): Promise<JobRun[]> {
  return sql()<JobRun[]>`SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ${limit}`;
}

export async function lastRunPerTask(): Promise<JobRun[]> {
  return sql()<JobRun[]>`
    SELECT DISTINCT ON (task) * FROM job_runs ORDER BY task, started_at DESC`;
}

export interface QualityCheck {
  id: string;
  check_type: string | null;
  ok: boolean;
  severity: string | null;
  failed: number;
  comparisons: number;
  failed_periods: string[];
  message: string | null;
  as_of: string | null;
}

export async function qualityChecks(): Promise<QualityCheck[]> {
  return sql()<QualityCheck[]>`
    SELECT * FROM quality_checks ORDER BY ok, severity NULLS LAST, id`;
}

export interface Delivery {
  delivery_id: string;
  report_type: string;
  edition: string;
  version: number | null;
  channel: string;
  recipient_id: string;
  recipient_hint: string | null;
  status: string;
  attempts: number;
  subject: string | null;
  sent_at: string | null;
  updated_at: string | null;
  last_error: string | null;
}

export async function deliveries(limit = 50): Promise<Delivery[]> {
  return sql()<Delivery[]>`SELECT * FROM deliveries ORDER BY updated_at DESC NULLS LAST LIMIT ${limit}`;
}

export async function meta<T = unknown>(key: string): Promise<T | null> {
  const rows = await sql()<{ value: T }[]>`SELECT value FROM meta WHERE key = ${key}`;
  return rows[0]?.value ?? null;
}

/** Leases currently held or lapsed, for the system page. */
export async function locks() {
  return sql()<
    { name: string; holder: string; acquired_at: string; expires_at: string; expired: boolean }[]
  >`SELECT name, holder, acquired_at, expires_at, expires_at < now() AS expired FROM job_locks`;
}

/** Whether the read model has ever been published, so an empty dashboard can explain itself. */
export async function isPopulated(): Promise<boolean> {
  try {
    const rows = await sql()<{ n: number }[]>`SELECT count(*)::int AS n FROM indicators`;
    return (rows[0]?.n ?? 0) > 0;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------ definitions

export interface ScorecardRow {
  key: string;
  label: string;
  series_id: string;
  dims: Record<string, string>;
  change: string | null;
  change_kind: string | null;
  good: "up" | "down" | "neutral";
}

export interface SectorDef {
  key: string;
  label: string;
  credit_series: string | null;
  activity_series: string[];
  bank_business_series: string | null;
}

export interface Definitions {
  scorecard: ScorecardRow[];
  sectors: SectorDef[];
  chart_window_months: number;
  report_titles: Record<string, string>;
  status_label: string | null;
}

/**
 * The headline metrics the deck already validates, rather than a list chosen for the screen.
 *
 * Returns null when the worker has not published them yet, so a page can say the dashboard is
 * waiting for its first run instead of inventing a scorecard of its own.
 */
export async function definitions(): Promise<Definitions | null> {
  return meta<Definitions>("definitions");
}

/**
 * The newest reading of each scorecard series, with the change the deck reports beside it.
 *
 * Each row is fetched on its own terms, because the scorecard's dimensions are per-row: the pricing
 * spread is published for AZN and for FX and the deck asks for the AZN leg specifically. An earlier
 * version ignored `dims` and took whichever slice the database returned first, so the tile labelled
 * "AZN" showed the FX spread — the right number under the wrong label, which is the failure this
 * system exists to prevent and the hardest kind to notice.
 *
 * A series that has several slices and no `dims` to choose between them returns no figure at all,
 * with the reason, rather than one of them picked arbitrarily.
 */
export interface ScorecardCell {
  row: ScorecardRow;
  current: (SeriesMeta & Point) | null;
  previous: Point | null;
  /** Set when no figure could be shown honestly; displayed instead of a number. */
  unavailable?: string;
}

export async function scorecard(): Promise<ScorecardCell[]> {
  const defs = await definitions();
  if (!defs?.scorecard?.length) return [];
  return Promise.all(defs.scorecard.map((row) => scorecardCell(row)));
}

async function scorecardCell(row: ScorecardRow): Promise<ScorecardCell> {
  const client = sql();
  const hasDims = row.dims && Object.keys(row.dims).length > 0;

  if (!hasDims) {
    const slices = await client<{ n: number }[]>`
      SELECT count(DISTINCT dims)::int AS n FROM indicators
      WHERE series_id = ${row.series_id} AND kind IN ('observation', 'policy')`;
    if ((slices[0]?.n ?? 0) > 1) {
      return {
        row, current: null, previous: null,
        unavailable:
          "This series is published for more than one dimension and the scorecard does not say "
          + "which. No figure is shown rather than an arbitrary slice.",
      };
    }
  }

  const rows = await client<(SeriesMeta & Point & { rn: number })[]>`
    SELECT * FROM (
      SELECT series_id, label, unit, kind, origin, period_type, formula, source_id,
             period_end, value, published_at, period_end AS latest_period,
             -- ::int matters: a bigint comes back from the driver as a string, and every
             -- comparison against a number below would then silently be false.
             row_number() OVER (ORDER BY period_end DESC)::int AS rn
      FROM indicators
      WHERE series_id = ${row.series_id} AND kind IN ('observation', 'policy')
        ${hasDims ? client`AND dims @> ${client.json(row.dims)}::jsonb` : client``}
    ) t WHERE rn <= 13`;

  const current = rows.find((r) => r.rn === 1) ?? null;
  // `lag12` compares with a year earlier, `lag1` with the previous period. Anything else — a
  // comparison against the prior edition — needs the edition record, so no change is shown rather
  // than a different comparison presented under the same label.
  const wanted = row.change === "lag12" ? 13 : row.change === "lag1" ? 2 : null;
  const previous = wanted ? rows.find((r) => r.rn === wanted) ?? null : null;
  return { row, current, previous };
}
