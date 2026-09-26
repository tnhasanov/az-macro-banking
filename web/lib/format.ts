/**
 * Formatting, with the provenance attached.
 *
 * The brief for this system is that no figure appears without its source and reporting period, so
 * the caption builders live next to the number formatters and take the same row. It is harder to
 * render a bare number than a sourced one, which is the intent.
 */
import type { Kind, SeriesMeta } from "./db";

const SOURCE_NAMES: Record<string, string> = {
  cba: "Central Bank of Azerbaijan",
  ssc: "State Statistical Committee",
  azstat: "State Statistical Committee",
  stat: "State Statistical Committee",
};

/** The publishing body behind a series id or source id, spelled out rather than abbreviated. */
export function sourceName(sourceId: string | null | undefined, seriesId?: string): string {
  const key = (sourceId ?? seriesId ?? "").split(/[._]/)[0]?.toLowerCase();
  return SOURCE_NAMES[key] ?? (sourceId ?? "unattributed source");
}

export function num(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const d = digits ?? (abs >= 1000 ? 0 : abs >= 100 ? 1 : 2);
  return value.toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/** A figure with its unit, at the precision that unit deserves. */
export function withUnit(value: number | null | undefined, unit: string | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const u = (unit ?? "").trim();
  if (u === "%" ) return `${num(value, 1)}%`;
  if (u === "pp") return `${signed(value, 1)} pp`;
  if (/mln/i.test(u)) return `${num(value, 0)} ${u}`;
  if (/index/i.test(u)) return num(value, 1);
  return u ? `${num(value, 1)} ${u}` : num(value, 2);
}

export function signed(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${num(Math.abs(value), digits)}`;
}

/**
 * A reporting period as an analyst would write it.
 *
 * Driven by the *first token* of the period type, never by a substring search. The vocabulary makes
 * that distinction load-bearing: `monthly_index_vs_same_month_prev_year` is a month whose value
 * compares with a year earlier, and a loose test for "year" in it would label a monthly CPI reading
 * as an annual one — a figure that is off by a factor nobody would spot. `annualised_ratio` is the
 * same trap in the other direction: a monthly observation of an annualised rate, not a year.
 */
export function period(periodEnd: string | null | undefined, periodType?: string | null): string {
  if (!periodEnd) return "period not recorded";
  const d = new Date(periodEnd);
  if (Number.isNaN(d.getTime())) return String(periodEnd);
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth();
  const quarter = `Q${Math.floor(m / 3) + 1} ${y}`;
  const month = `${MONTHS[m]} ${y}`;

  switch ((periodType ?? "").split("_")[0].toLowerCase()) {
    case "ytd":
      return `Jan–${MONTHS[m]} ${y} (cumulative)`;
    case "annual":
      return String(y);
    case "quarter":
    case "quarterly":
      return quarter;
    case "rolling3":
      return `3 months to ${MONTHS_SHORT[m]} ${y}`;
    case "rolling4":
      return `4 quarters to ${quarter}`;
    case "policy":
      return `effective ${dateLong(periodEnd)}`;
    case "forecast":
      return `projected for ${month}`;
    case "stress":
      return `stress horizon ${month}`;
    // month, monthly, annualised, stock, period — all readings at a month end.
    default:
      return month;
  }
}

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function dateLong(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return `${d.getUTCDate()} ${MONTHS_SHORT[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

export function dateShort(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return `${MONTHS_SHORT[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
}

/** Timestamps are shown in Asia/Baku, because that is the clock the schedule is written in. */
export function bakuTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return `${d.toLocaleString("en-GB", {
    timeZone: "Asia/Baku",
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  })} Baku`;
}

export function sinceNow(value: string | null | undefined): string {
  if (!value) return "never";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return String(value);
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function bytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "—";
  const units = ["B", "kB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export const KIND_LABEL: Record<Kind, string> = {
  observation: "Observed",
  forecast: "Forecast",
  stress: "Stress scenario",
  policy: "Policy decision",
};

/**
 * The provenance caption for one figure.
 *
 * Always three things: what period it is for, who published it, and — when the number was worked
 * out here rather than published — which formula produced it. The last one matters: a reader must
 * never attribute our arithmetic to the Central Bank.
 */
export function provenance(meta: Partial<SeriesMeta> & { period_end?: string | null }): string[] {
  const lines: string[] = [];
  const p = meta.period_end ?? meta.latest_period;
  lines.push(`${KIND_LABEL[(meta.kind as Kind) ?? "observation"]} · ${period(p, meta.period_type)}`);
  const who = sourceName(meta.source_id, meta.series_id);
  lines.push(
    meta.origin === "computed"
      ? `Computed here (${meta.formula ?? "derived"}) from ${who}`
      : `Source: ${who}`,
  );
  if (meta.published_at) lines.push(`Published ${dateLong(meta.published_at)}`);
  return lines;
}

/**
 * The name of a report type.
 *
 * `titles` comes from the definitions the worker publishes, which are the delivery layer's own
 * names — the ones that appear in the email subject line. The fallback is only for a type added to
 * the engine since the last run published, and it is deliberately ugly enough to notice.
 */
export function reportTitle(type: string, titles?: Record<string, string>): string {
  return (
    titles?.[type] ??
    type.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

export type Tone = "good" | "warning" | "serious" | "critical" | "neutral";

export function statusTone(status: string | null | undefined): Tone {
  switch ((status ?? "").toLowerCase()) {
    case "ok": case "sent": case "published": case "success": case "pass": case "complete":
      return "good";
    case "partial": case "unchanged": case "skipped": case "warning": case "pending": case "due":
      return "warning";
    case "needs_review": case "blocked": case "not_ready": case "stale":
      return "serious";
    case "failed": case "error": case "critical":
      return "critical";
    default:
      return "neutral";
  }
}
