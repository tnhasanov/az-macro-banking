"use client";

/**
 * The request form. It sends a report type and a few named choices; the server validates them
 * against the same rules as the worker and decides everything else. The button is disabled while
 * a request is in flight, and the server would fold a second identical request into the first
 * anyway, so a double click is harmless.
 */
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { postJSON } from "@/lib/client";
import { REPORT_TYPES, type ReportType } from "@/lib/params";

interface Offer {
  edition_id: string; report_type: string; edition: string; version: number; published_at: string; href: string;
  files: { name: string; role: string; bytes: number; href: string }[];
}

const TYPES = Object.entries(REPORT_TYPES) as [ReportType, { label: string }][];
const PUB_TYPES = new Set(["mpr_brief", "fsr_brief", "decision_update"]);

export function GenerateForm(props: {
  sectors: string[];
  latest: { month: string | null; week: { start: string; end: string } | null };
  weeks: string[];
  archivedMonths: string[];
  publications: Record<string, { publication_id: string; edition: string | null; published_at: string | null }[]>;
  canEmail: boolean;
  email: string | null;
  admin: boolean;
}) {
  const router = useRouter();
  const [reportType, setReportType] = useState<ReportType>("monthly");
  const [period, setPeriod] = useState("latest");
  const [sector, setSector] = useState(props.sectors[0] ?? "");
  const [publication, setPublication] = useState("latest");
  const [refresh, setRefresh] = useState<"latest_data" | "check_sources">("latest_data");
  const [emailMe, setEmailMe] = useState(false);
  const [force, setForce] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const periods = useMemo(() => {
    if (reportType === "weekly") {
      return [{ value: "latest", label: props.latest.week ? `Latest complete week (${props.latest.week.start})` : "Latest complete week" },
        ...props.weeks.slice(1).map((w) => ({ value: w, label: `Week of ${w}` }))];
    }
    if (reportType === "monthly" || reportType === "sector") {
      const months = new Set(props.archivedMonths);
      return [{ value: "latest", label: props.latest.month ? `Latest available (${props.latest.month})` : "Latest available" },
        ...[...months].filter((m) => m !== props.latest.month).sort().reverse().map((m) => ({ value: m, label: `${m} (archived)` }))];
    }
    return [];
  }, [reportType, props]);

  function params() {
    const p: Record<string, string> = { refresh };
    if (PUB_TYPES.has(reportType)) p.publication_id = publication;
    else p.period = period;
    if (reportType === "sector") p.sector = sector;
    return p;
  }

  async function submit(generateAnyway = false) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const res = await postJSON<{ offer?: Offer; note?: string; href?: string; dispatch?: string | null }>("/api/jobs", {
        report_type: reportType, params: params(), email_me: emailMe, generate_anyway: generateAnyway,
        ...(force ? { force_reason: reason } : {}),
      });
      if (res.offer) {
        setOffer(res.offer);
        setNote(res.note ?? null);
      } else if (res.href) {
        router.push(res.href);
        return;
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  async function emailOffer() {
    if (!offer) return;
    setBusy(true);
    setError(null);
    try {
      const res = await postJSON<{ note: string }>("/api/editions/send", { edition_id: offer.edition_id });
      setNote(res.note);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  return (
    <form onSubmit={(e) => { e.preventDefault(); void submit(false); }} className="stack" style={{ gap: 16 }}>
      <div className="field">
        <label htmlFor="report-type">Report</label>
        <select id="report-type" value={reportType} onChange={(e) => {
          setReportType(e.target.value as ReportType); setPeriod("latest"); setPublication("latest"); setOffer(null);
        }}>
          {TYPES.map(([id, spec]) => <option key={id} value={id}>{spec.label}</option>)}
        </select>
      </div>

      {reportType === "sector" && (
        <div className="field">
          <label htmlFor="sector">Sector</label>
          <select id="sector" value={sector} onChange={(e) => { setSector(e.target.value); setOffer(null); }}>
            {props.sectors.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      )}

      {PUB_TYPES.has(reportType) ? (
        <div className="field">
          <label htmlFor="publication">Source publication or decision</label>
          <select id="publication" value={publication} onChange={(e) => { setPublication(e.target.value); setOffer(null); }}>
            <option value="latest">Latest released</option>
            {(props.publications[reportType] ?? []).map((p) => (
              <option key={p.publication_id} value={p.publication_id}>
                {p.edition ?? p.publication_id}{p.published_at ? ` (released ${p.published_at.slice(0, 10)})` : ""}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div className="field">
          <label htmlFor="period">Period</label>
          <select id="period" value={period} onChange={(e) => { setPeriod(e.target.value); setOffer(null); }}>
            {periods.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
          {(reportType === "monthly" || reportType === "sector") && (
            <span className="muted" style={{ fontSize: 12 }}>
              Reports are built from the current information set. An earlier month is available as its archived edition.
            </span>
          )}
        </div>
      )}

      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink-secondary)", marginBottom: 6 }}>Data</legend>
        <div className="choice-grid">
          <label className="choice">
            <input type="radio" name="refresh" checked={refresh === "latest_data"} onChange={() => { setRefresh("latest_data"); setOffer(null); }} />
            <span>Use the latest collected data<span className="sub">Fastest. Uses what the last source check collected.</span></span>
          </label>
          <label className="choice">
            <input type="radio" name="refresh" checked={refresh === "check_sources"} onChange={() => { setRefresh("check_sources"); setOffer(null); }} />
            <span>Check sources for updates first<span className="sub">Downloads from the CBA and SSC before producing; takes longer.</span></span>
          </label>
        </div>
      </fieldset>

      <label className="check">
        <input type="checkbox" checked={emailMe} disabled={!props.canEmail} onChange={(e) => setEmailMe(e.target.checked)} />
        <span>Email me when it is ready
          <span className="sub">{props.canEmail ? `To ${props.email}, and nobody else.`
            : "This sign-in has no email address configured (AZMONITOR_OWNER_EMAIL)."}</span></span>
      </label>

      {props.admin && (
        <div className="stack" style={{ gap: 8 }}>
          <label className="check">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
            <span>Force a new version<span className="sub">Administrators only. Produces a new version even when nothing
              changed, is never announced to subscribers, and the reason is recorded.</span></span>
          </label>
          {force && (
            <div className="field">
              <label htmlFor="reason">Reason (recorded in the audit log)</label>
              <textarea id="reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500}
                placeholder="For example: layout fix to the deposits slide" />
            </div>
          )}
        </div>
      )}

      <div className="row" style={{ gap: 10 }}>
        <button type="submit" className="primary" disabled={busy || (force && reason.trim().length < 10)}>
          {busy ? "Working…" : force ? "Regenerate" : "Generate"}
        </button>
        {error && <span className="error-text" role="alert">{error}</span>}
      </div>

      {offer && (
        <div className="notice" role="status">
          <strong>An identical validated edition already exists.</strong>
          <div style={{ marginTop: 6, fontSize: 13.5 }}>
            {REPORT_TYPES[offer.report_type as ReportType]?.label ?? offer.report_type} {offer.edition}, version {offer.version},
            published {offer.published_at.slice(0, 16).replace("T", " ")} UTC. Its inputs have not changed since.
          </div>
          <div className="row" style={{ gap: 8, marginTop: 10 }}>
            <a className="button" href={offer.href}>Open it</a>
            {offer.files.map((f) => <a key={f.href} className="button" href={f.href}>{f.role.toUpperCase()}</a>)}
            {props.canEmail && <button type="button" onClick={() => void emailOffer()} disabled={busy}>Email it to me</button>}
            <button type="button" onClick={() => void submit(true)} disabled={busy}>Generate anyway</button>
          </div>
          {note && <div className="muted" style={{ marginTop: 8, fontSize: 12.5 }}>{note}</div>}
        </div>
      )}
    </form>
  );
}
