/**
 * What an email says. Everything in it comes from the publication record the worker wrote in the
 * same transaction that published the edition: the findings that passed grounding, the reporting
 * period of every input, the information cutoff, the stated limitations. Nothing is summarised or
 * reworded at send time, so the email cannot say something the report does not.
 *
 * Links are to the application, never to storage: a report page and the authenticated download
 * route. A private blob has no URL worth sending, and a storage credential never leaves the server.
 */
import { REPORT_TYPES, isReportType } from "../params";
import type { Publication } from "../appstate";

export interface Composed {
  subject: string;
  text: string;
  html: string;
  headers: Record<string, string>;
}

export interface ChangeNote {
  classification: string;
  dataset_id: string | null;
  publication_id: string | null;
  periods: string[];
  published_at: string | null;
}

const PERIOD_LABELS: Record<string, string> = {
  banking_period: "Banking data", cpi_period: "Consumer prices", gdp_period: "GDP", macro_period: "Macro headline",
  window_start: "Window from", window_end: "Window to", edition_month: "Edition month",
};

export function title(reportType: string): string {
  return isReportType(reportType) ? REPORT_TYPES[reportType].label : reportType;
}

export function editionLabel(p: Pick<Publication, "report_type" | "edition" | "manifest">): string {
  if (p.report_type === "weekly" && p.manifest?.window?.start) {
    return `week of ${p.manifest.window.start}${p.manifest.window.end ? ` to ${p.manifest.window.end}` : ""}`;
  }
  if (p.manifest?.publication?.edition) return String(p.manifest.publication.edition);
  if (p.report_type === "sector") {
    const [sector, month] = p.edition.split(/_(?=\d{4}-\d{2}$)/);
    return `${sector} - ${month ?? ""}`.trim();
  }
  return p.edition;
}

function why(purpose: string, p: Publication, changes: ChangeNote[]): string {
  const releases = changes.filter((c) => ["new_observations", "new_publication", "substantive_revision"]
    .includes(c.classification));
  const named = releases.slice(0, 4).map((c) => {
    const what = c.publication_id ?? c.dataset_id ?? "a source";
    const periods = c.periods.length ? ` for ${c.periods.slice(-2).join(", ")}` : "";
    const when = c.published_at ? `, released ${bakuDate(c.published_at).slice(0, 10)}` : "";
    return `${what}${periods}${when}`;
  });
  switch (purpose) {
    case "new_edition":
      if (p.cause === "weekly") return "This is the scheduled digest of the week's official releases.";
      if (p.cause === "new_publication") return `The source published something new: ${named.join("; ") || "a new publication"}.`;
      return `New official data arrived: ${named.join("; ") || "new observations"}.`;
    case "revised_edition":
      return `Figures already published were revised, so this edition was reissued as version ${p.version}`
        + `${named.length ? `: ${named.join("; ")}` : ""}.`;
    case "manual_request":
      return "You asked to be emailed when this report was ready.";
    case "manual_resend":
      return "You asked for a copy of this report.";
    default:
      return "";
  }
}

/** The database driver returns timestamps as Date objects; everything here wants text. */
function utcStamp(v: unknown): string {
  const d = v instanceof Date ? v : new Date(String(v));
  return Number.isNaN(d.getTime()) ? String(v) : `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function bakuDate(v: unknown): string {
  const d = v instanceof Date ? v : new Date(String(v));
  if (Number.isNaN(d.getTime())) return String(v);
  return new Date(d.getTime() + 4 * 3600_000).toISOString().slice(0, 10);
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function compose(purpose: string, p: Publication, opts: {
  baseUrl: string; changes?: ChangeNote[]; attached?: string | null; attachmentNote?: string | null;
  unsubscribeUrl?: string | null;
}): Composed {
  const name = title(p.report_type);
  const label = editionLabel(p);
  const subject = {
    new_edition: `${name}: ${label}`,
    revised_edition: `${name}: ${label} (revised, version ${p.version})`,
    manual_request: `Your report is ready: ${name}, ${label}`,
    manual_resend: `${name}: ${label} (copy you requested)`,
  }[purpose] ?? `${name}: ${label}`;

  const page = `${opts.baseUrl}/reports/${encodeURIComponent(p.report_type)}/${encodeURIComponent(p.edition)}/${p.version}`;
  const files = (p.manifest.files ?? []).filter((f) => ["pdf", "pptx", "xlsx"].includes(f.role));
  const periods = Object.entries(p.reporting_periods ?? {}).filter(([, v]) => v)
    .map(([k, v]) => `${PERIOD_LABELS[k] ?? k.replace(/_/g, " ")}: ${v}`);
  const cutoff = p.information_cutoff ? `Information cutoff: end of ${bakuDate(p.information_cutoff)} (Asia/Baku)` : null;
  const findings = (p.findings ?? []).slice(0, 5);
  const reason = why(purpose, p, opts.changes ?? []);
  const limitations = [...(p.limitations ?? [])];
  if (opts.attachmentNote) limitations.push(opts.attachmentNote);

  const text = [
    `${name} - ${label}`,
    `Edition ${p.edition}, version ${p.version}, published ${utcStamp(p.published_at)}`,
    "",
    reason,
    "",
    findings.length ? "Key findings (as validated in the report):" : "",
    ...findings.map((f) => `  - ${f}`),
    "",
    "Reporting periods:",
    ...periods.map((x) => `  - ${x}`),
    ...(cutoff ? [cutoff] : []),
    "",
    `Open the report: ${page}`,
    ...files.map((f) => `  ${f.role.toUpperCase()}: ${opts.baseUrl}/api/files/${f.key}`),
    "(Links require signing in to the dashboard.)",
    ...(opts.attached ? [`The PDF (${opts.attached}) is attached.`] : []),
    "",
    ...(limitations.length ? ["Limitations:", ...limitations.map((l) => `  - ${l}`), ""] : []),
    "Generated by the Azerbaijan Macro & Banking Monitor from official CBA and SSC publications.",
    ...(opts.unsubscribeUrl ? [`Stop these emails: ${opts.unsubscribeUrl}`] : []),
  ].filter((line, i, all) => !(line === "" && all[i - 1] === "")).join("\n");

  const li = (items: string[]) => items.map((x) => `<li>${esc(x)}</li>`).join("");
  const html = `<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2933;line-height:1.5;max-width:640px">
<h2 style="font-size:18px;margin:0 0 4px">${esc(name)} &ndash; ${esc(label)}</h2>
<div style="color:#52606d;font-size:12.5px">Edition ${esc(p.edition)}, version ${p.version}, published ${esc(utcStamp(p.published_at))}</div>
${reason ? `<p>${esc(reason)}</p>` : ""}
${findings.length ? `<h3 style="font-size:14px;margin:16px 0 4px">Key findings</h3><ul>${li(findings)}</ul>` : ""}
<h3 style="font-size:14px;margin:16px 0 4px">Reporting periods</h3><ul>${li(periods)}</ul>
${cutoff ? `<p style="color:#52606d;font-size:12.5px">${esc(cutoff)}</p>` : ""}
<p><a href="${esc(page)}" style="background:#1d4ed8;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none">Open the report</a></p>
<p style="font-size:13px">${files.map((f) => `<a href="${esc(`${opts.baseUrl}/api/files/${f.key}`)}">${esc(f.role.toUpperCase())}</a>`).join(" &middot; ")}
<br><span style="color:#52606d;font-size:12px">Links require signing in to the dashboard.${opts.attached ? ` The PDF is attached.` : ""}</span></p>
${limitations.length ? `<h3 style="font-size:14px;margin:16px 0 4px">Limitations</h3><ul style="color:#52606d">${li(limitations)}</ul>` : ""}
<p style="color:#7b8794;font-size:11.5px;border-top:1px solid #e4e7eb;padding-top:8px">Generated by the Azerbaijan Macro &amp; Banking Monitor from official CBA and SSC publications.
${opts.unsubscribeUrl ? `<br><a href="${esc(opts.unsubscribeUrl)}" style="color:#7b8794">Stop these emails</a>` : ""}</p>
</body></html>`;

  const headers: Record<string, string> = {};
  if (opts.unsubscribeUrl) {
    headers["List-Unsubscribe"] = `<${opts.unsubscribeUrl}>`;
    headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click";
  }
  return { subject, text, html, headers };
}

export function composeTest(baseUrl: string): Composed {
  const text = "This is a test message from the Azerbaijan Macro & Banking Monitor.\n\n"
    + "If you received it, email delivery from this deployment works. No report is attached.\n\n"
    + `Dashboard: ${baseUrl}`;
  return {
    subject: "Test message from the Azerbaijan Macro & Banking Monitor",
    text,
    html: `<p>${esc(text).replace(/\n\n/g, "</p><p>")}</p>`,
    headers: {},
  };
}
