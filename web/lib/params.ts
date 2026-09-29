/**
 * What a report request may ask for — the TypeScript twin of azmonitor/jobs/params.py.
 *
 * Both sides apply the same rules to the same cases (tests/fixtures/request-cases.json is read by
 * the Python and the Node tests alike), so the browser form, this API and the worker cannot
 * disagree about what a valid request is. The worker re-validates what it reads from the job row,
 * so a request that somehow bypassed this file is still refused before the engine runs.
 */
export const REPORT_TYPES = {
  monthly: { label: "Monthly Monitor", period: "month" },
  weekly: { label: "Weekly Digest", period: "week" },
  sector: { label: "Sector Review", period: "month", sector: true },
  mpr_brief: { label: "MPR Brief", publicationType: "monetary_policy_review" },
  fsr_brief: { label: "FSR Brief", publicationType: "financial_stability_report" },
  decision_update: { label: "Policy Decision Update", publicationType: "policy_decision" },
} as const;

export type ReportType = keyof typeof REPORT_TYPES;

/** The configured sectors (config/reports.yaml). tests/test_request_params.py checks they agree. */
export const SECTORS = ["agriculture", "construction", "trade", "transport", "industry"] as const;
export const REFRESH = ["latest_data", "check_sources"] as const;
const ALLOWED_KEYS = new Set(["period", "sector", "publication_id", "refresh"]);

const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const PUBLICATION = /^[a-z_]{3,40}:[A-Za-z0-9_.-]{1,60}$/;

export interface Params {
  refresh: (typeof REFRESH)[number];
  period?: string;
  sector?: string;
  publication_id?: string;
}

export class InvalidRequest extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export function isReportType(value: unknown): value is ReportType {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(REPORT_TYPES, value);
}

/** The canonical parameters, or InvalidRequest. `sectors` is the configured list. */
export function normalise(reportType: string, raw: Record<string, unknown> | null | undefined,
  sectors: readonly string[]): Params {
  if (!isReportType(reportType)) {
    throw new InvalidRequest("unknown_report", `There is no report type called '${reportType}'.`);
  }
  const input = { ...(raw ?? {}) } as Record<string, unknown>;
  for (const k of Object.keys(input)) {
    if (input[k] === undefined || input[k] === null || input[k] === "") delete input[k];
  }
  const extra = Object.keys(input).filter((k) => !ALLOWED_KEYS.has(k)).sort();
  if (extra.length) {
    throw new InvalidRequest("unknown_parameter", `Unexpected parameter(s): ${extra.join(", ")}.`);
  }
  const spec = REPORT_TYPES[reportType] as { label: string; period?: string; sector?: boolean;
    publicationType?: string };
  const refresh = (input.refresh ?? "latest_data") as string;
  if (!(REFRESH as readonly string[]).includes(refresh)) {
    throw new InvalidRequest("invalid_refresh",
      "Choose either the latest collected data or a source check first.");
  }
  const out: Params = { refresh: refresh as Params["refresh"] };

  if (spec.publicationType) {
    if (input.period !== undefined && input.period !== "latest") {
      throw new InvalidRequest("unsupported_period",
        `A ${spec.label} is about one publication; choose the publication, not a period.`);
    }
    const pub = String(input.publication_id ?? "latest");
    if (pub !== "latest" && (!PUBLICATION.test(pub) || !pub.startsWith(`${spec.publicationType}:`))) {
      throw new InvalidRequest("invalid_publication", `'${pub}' is not a ${spec.label} source publication.`);
    }
    out.publication_id = pub;
    if (input.sector !== undefined) {
      throw new InvalidRequest("unknown_parameter", "A sector applies only to a Sector Review.");
    }
    return out;
  }

  const period = String(input.period ?? "latest");
  if (period !== "latest") {
    if (spec.period === "month" && !MONTH.test(period)) {
      throw new InvalidRequest("invalid_period", "A monthly period is written YYYY-MM, for example 2026-07.");
    }
    if (spec.period === "week") {
      if (!DAY.test(period)) {
        throw new InvalidRequest("invalid_period", "A week is named by its Monday, written YYYY-MM-DD.");
      }
      const d = new Date(`${period}T00:00:00Z`);
      if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== period) {
        throw new InvalidRequest("invalid_period", `${period} is not a date.`);
      }
      if (d.getUTCDay() !== 1) {
        const name = d.toLocaleDateString("en-GB", { weekday: "long", timeZone: "UTC" });
        throw new InvalidRequest("invalid_period", `${period} is a ${name}; a week is named by its Monday.`);
      }
    }
  }
  out.period = period;

  if (spec.sector) {
    const sector = input.sector;
    if (!sector) throw new InvalidRequest("missing_sector", "Choose the sector to review.");
    if (typeof sector !== "string" || !sectors.includes(sector)) {
      throw new InvalidRequest("unknown_sector",
        `There is no configured sector '${String(sector)}'; choose one of ${sectors.join(", ")}.`);
    }
    out.sector = sector;
  } else if (input.sector !== undefined) {
    throw new InvalidRequest("unknown_parameter", "A sector applies only to a Sector Review.");
  }
  if (input.publication_id !== undefined) {
    throw new InvalidRequest("unknown_parameter", "A source publication applies only to the briefs.");
  }
  return out;
}

export function scopeOf(params: Params): string {
  return params.sector ?? params.publication_id ?? params.period ?? "latest";
}

export function describe(reportType: string, params: Partial<Params>): string {
  const spec = isReportType(reportType) ? REPORT_TYPES[reportType] : { label: reportType };
  const bits: string[] = [spec.label];
  if (params.sector) bits.push(params.sector);
  if (params.publication_id && params.publication_id !== "latest") bits.push(params.publication_id.split(":").slice(1).join(":"));
  if (params.period && params.period !== "latest") bits.push(params.period);
  return bits.join(" - ");
}
