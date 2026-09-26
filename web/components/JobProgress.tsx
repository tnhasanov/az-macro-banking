"use client";

/**
 * Following one job. Polls the job's API while it is active and stops once it reaches an outcome.
 *
 * Progress is shown as the stages the job has actually passed through, with the detail the worker
 * reported ("dataset 12 of 31", "PDF") — never a percentage, because the engine has no honest way
 * to know one. A job whose worker has stopped reporting is flagged as such, rather than left
 * looking busy.
 */
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { postJSON } from "@/lib/client";
import type { JobView } from "@/lib/jobview";

const OUTCOME: Record<string, { tone: string; title: string }> = {
  succeeded: { tone: "good", title: "Published" },
  reused: { tone: "good", title: "An identical edition already exists" },
  unchanged: { tone: "neutral", title: "Nothing new" },
  waiting_for_data: { tone: "warning", title: "Waiting for data" },
  blocked: { tone: "serious", title: "Blocked before publication" },
  failed: { tone: "critical", title: "Failed" },
  cancelled: { tone: "neutral", title: "Cancelled" },
};

function when(v: string | null | undefined) {
  if (!v) return "—";
  const d = new Date(v);
  return d.toLocaleString("en-GB", { timeZone: "Asia/Baku", dateStyle: "medium", timeStyle: "short" }) + " Baku";
}

function ago(seconds: number | null) {
  if (seconds === null) return "never";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  return `${Math.round(seconds / 60)} min ago`;
}

function kb(n: number) {
  return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`;
}

export function JobProgress({ initial, canEmail }: { initial: JobView; canEmail: boolean }) {
  const [view, setView] = useState<JobView>(initial);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(Date.now());

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`/api/jobs/${initial.job.job_id}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`The server answered ${response.status}.`);
      setView(await response.json());
      setError(null);
    } catch (e) {
      setError(`Could not refresh: ${e instanceof Error ? e.message : String(e)} Retrying.`);
    }
  }, [initial.job.job_id]);

  useEffect(() => {
    if (view.job.terminal && view.children.every((c) => !["queued", "dispatched", "running"].includes(c.status))) return;
    const elapsed = Date.now() - started.current;
    const delay = elapsed < 120_000 ? 3000 : elapsed < 600_000 ? 6000 : 15000;
    const t = setTimeout(() => void refresh(), delay);
    return () => clearTimeout(t);
  }, [view, refresh]);

  async function act(path: string, body: unknown = {}) {
    setBusy(true);
    setNote(null);
    try {
      const res = await postJSON<{ note?: string; href?: string }>(path, body);
      if (res.href && res.href !== `/jobs/${view.job.job_id}`) {
        window.location.href = res.href;
        return;
      }
      setNote(res.note ?? "Done.");
      await refresh();
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  const j = view.job;
  const outcome = OUTCOME[j.status];
  const active = !j.terminal;
  const failedStage = ["failed", "blocked"].includes(j.status) ? j.stage : null;

  return (
    <div className="stack">
      {error && <div className="notice">{error}</div>}

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <section className="card">
          <header className="card-head">
            <div>
              <h3 className="card-title">{outcome ? outcome.title : j.status === "running" ? "In progress" : j.status === "dispatched" ? "Starting a runner" : "Queued"}</h3>
              <div className="card-note">
                {j.status === "queued" && j.next_attempt_at ? `Next attempt ${when(j.next_attempt_at)}. ` : ""}
                {j.stage_detail && active ? j.stage_detail : null}
              </div>
            </div>
            <span className={`badge badge-${outcome?.tone ?? "neutral"}`}>{j.status.replace(/_/g, " ")}</span>
          </header>

          <ol className="stages" aria-label="Stages">
            {view.stages.map((s) => {
              const state = failedStage && view.stages.find((x) => x.state === "current")?.id === s.id ? "failed"
                : j.terminal && s.state === "current" ? "done" : s.state;
              return (
                <li key={s.id} data-state={state}>
                  <span className="dot" aria-hidden />
                  <span>
                    {s.label}
                    {s.state === "skipped" && <span className="sr-only"> (not part of this job)</span>}
                    {s.state === "current" && active && (
                      <span className="detail">{j.stage_label ?? j.stage_detail ?? "working"}</span>
                    )}
                  </span>
                </li>
              );
            })}
          </ol>

          {j.stale && (
            <div className="notice" style={{ marginTop: 12 }}>
              <strong>The worker has not reported for {ago(j.heartbeat_age_seconds)}.</strong>
              <div style={{ marginTop: 4 }}>If it has stopped, its lease lapses within five minutes and the job is
                retried automatically.</div>
            </div>
          )}

          {j.error_message && (
            <div className="notice" style={{ marginTop: 12 }}>
              <strong>{j.status === "waiting_for_data" ? "Why it is waiting" : j.status === "reused" || j.status === "unchanged" ? "Note" : "What went wrong"}</strong>
              <div style={{ marginTop: 4 }}>{j.error_message}</div>
              {j.error_code && <div className="muted mono" style={{ fontSize: 11.5, marginTop: 4 }}>{j.error_code}</div>}
            </div>
          )}
          {typeof j.result?.message === "string" && !j.error_message && (
            <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>{j.result.message as string}</p>
          )}

          <div className="row" style={{ gap: 8, marginTop: 14 }}>
            {active && !j.cancel_requested && (
              <button type="button" onClick={() => void act(`/api/jobs/${j.job_id}/cancel`)} disabled={busy}>Cancel</button>
            )}
            {["failed", "blocked", "cancelled", "waiting_for_data"].includes(j.status) && (
              <button type="button" onClick={() => void act(`/api/jobs/${j.job_id}/retry`)} disabled={busy}>Retry</button>
            )}
            {canEmail && !j.notify_requester && (active || view.edition) && j.kind === "report" && (
              <button type="button" onClick={() => void act(`/api/jobs/${j.job_id}/notify`)} disabled={busy}>
                {active ? "Email me when it is ready" : "Email it to me"}
              </button>
            )}
            {j.notify_requester && active && <span className="muted" style={{ fontSize: 12.5 }}>You will be emailed when it is published.</span>}
            {note && <span className="muted" style={{ fontSize: 12.5 }}>{note}</span>}
          </div>
        </section>

        <section className="card">
          <header className="card-head"><h3 className="card-title">Request</h3></header>
          <table className="data">
            <tbody>
              <tr><td>Job</td><td className="mono">{j.job_id}</td></tr>
              <tr><td>Report</td><td>{j.description}</td></tr>
              {Object.entries(j.params ?? {}).map(([k, v]) => (
                <tr key={k}><td>{k.replace(/_/g, " ")}</td><td>{String(v).replace(/_/g, " ")}</td></tr>
              ))}
              <tr><td>Trigger</td><td>{j.trigger.replace(/_/g, " ")}{j.force_reason ? ` — ${j.force_reason}` : ""}</td></tr>
              <tr><td>Requested</td><td>{when(j.requested_at)} by {j.requested_by}</td></tr>
              <tr><td>Started</td><td>{when(j.started_at)}</td></tr>
              <tr><td>Finished</td><td>{when(j.finished_at)}</td></tr>
              <tr><td>Attempt</td><td>{j.attempt} of {j.max_attempts}</td></tr>
              <tr><td>Last heartbeat</td><td>{j.heartbeat_at ? `${when(j.heartbeat_at)} (${ago(j.heartbeat_age_seconds)})` : "—"}</td></tr>
              <tr><td>Runner</td><td>{j.run_url ? <a href={j.run_url} target="_blank" rel="noreferrer">GitHub Actions run</a> : "—"}</td></tr>
              {j.parent_job_id && <tr><td>Started by</td><td><Link href={`/jobs/${j.parent_job_id}`}>a source check</Link></td></tr>}
            </tbody>
          </table>
        </section>
      </div>

      {view.edition && (
        <section className="card">
          <header className="card-head">
            <div>
              <h3 className="card-title"><Link href={view.edition.href}>{view.edition.edition}, version {view.edition.version}</Link></h3>
              <div className="card-note">
                Published {when(view.edition.published_at)} · cause: {view.edition.cause.replace(/_/g, " ")}
                {view.edition.supersedes ? ` · revises ${view.edition.supersedes}` : ""}
              </div>
            </div>
          </header>
          <div className="row" style={{ gap: 8 }}>
            {view.edition.files.map((f) => (
              <a key={f.href} className="button" href={f.href}>{f.role.toUpperCase()} <span className="muted" style={{ fontWeight: 400 }}>{kb(f.bytes)}</span></a>
            ))}
          </div>
          {view.edition.findings.length > 0 && (
            <ul style={{ margin: "12px 0 0", paddingLeft: 18, fontSize: 13.5 }}>
              {view.edition.findings.map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          )}
          <div className="grid grid-2" style={{ marginTop: 12 }}>
            <div>
              <div className="stat-label">Reporting periods</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {Object.entries(view.edition.reporting_periods ?? {}).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ")}: {String(v)}</li>)}
              </ul>
              {view.edition.information_cutoff && <div className="prov-line">Information cutoff {when(view.edition.information_cutoff)}</div>}
            </div>
            <div>
              <div className="stat-label">Validation</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {view.edition.checks.map((c) => <li key={c.id}>{c.ok ? "✓" : "✗"} {c.id.replace(/_/g, " ")}: {c.detail}</li>)}
              </ul>
            </div>
          </div>
          {view.edition.limitations.length > 0 && (
            <div className="stat-prov">{view.edition.limitations.join(" ")}</div>
          )}
        </section>
      )}

      {view.children.length > 0 && (
        <section className="card">
          <header className="card-head"><h3 className="card-title">Reports this check produced</h3></header>
          <table className="data">
            <tbody>
              {view.children.map((c) => (
                <tr key={c.job_id}>
                  <td><Link href={`/jobs/${c.job_id}`}>{c.description}</Link></td>
                  <td><span className="badge badge-neutral">{c.status.replace(/_/g, " ")}</span></td>
                  <td>{c.edition_id ?? c.error_message ?? c.stage ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {Array.isArray(j.result?.waiting) && (j.result.waiting as unknown[]).length > 0 && (
        <section className="card">
          <header className="card-head"><h3 className="card-title">Waiting for data</h3></header>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13.5 }}>
            {(j.result.waiting as { report_type: string; scope: string; state: string; missing?: string[] }[]).map((w, i) => (
              <li key={i}>{w.report_type} ({w.scope}): {w.state}{w.missing?.length ? ` — waiting for ${w.missing.join(", ")}` : ""}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="card">
        <header className="card-head">
          <div>
            <h3 className="card-title">Email</h3>
            <div className="card-note">Separate from the report: a published edition stays published whatever happens to its email.</div>
          </div>
        </header>
        {view.emails.length === 0 ? (
          <p className="muted" style={{ fontSize: 13 }}>No email is associated with this job.</p>
        ) : (
          <table className="data">
            <thead><tr><th>To</th><th>Purpose</th><th>Status</th><th>Detail</th></tr></thead>
            <tbody>
              {view.emails.map((e) => (
                <tr key={e.delivery_id}>
                  <td>{e.to}</td>
                  <td>{e.purpose.replace(/_/g, " ")}</td>
                  <td><span className="badge badge-neutral">{e.status}</span></td>
                  <td className="muted" style={{ fontSize: 12.5 }}>
                    {e.status === "accepted" ? "Accepted by the provider; delivery not yet confirmed." : ""}
                    {e.status === "delivered" ? `Delivered ${when(e.delivered_at)}.` : ""}
                    {e.status_reason ?? ""} {e.last_error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <details className="card">
        <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13.5 }}>Event log ({view.events.length})</summary>
        <table className="data" style={{ marginTop: 8 }}>
          <tbody>
            {view.events.map((e, i) => (
              <tr key={i}>
                <td className="nowrap">{when(e.at)}</td>
                <td>{e.status}</td>
                <td>{e.stage?.replace(/_/g, " ")}</td>
                <td>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
