/**
 * Asking for a report.
 *
 * Beside the form: the period each report would cover if produced now, and when the sources were
 * last checked. Banking tables, national accounts and prices are published on different calendars,
 * so "the latest monthly" combines inputs of different months, and the page says which.
 */
import Link from "next/link";
import { ensureSchema, environment, meta, recentJobs, type Availability, type SourceCheck } from "@/lib/appstate";
import { sql } from "@/lib/db";
import { dispatcher } from "@/lib/dispatch";
import { viewer } from "@/lib/viewer";
import { Card, Empty, StatusBadge } from "@/components/ui";
import { GenerateForm } from "@/components/GenerateForm";
import { dateLong, sinceNow } from "@/lib/format";
import { describe, SECTORS } from "@/lib/params";

export const dynamic = "force-dynamic";

function previousMondays(n: number): string[] {
  const baku = new Date(Date.now() + 4 * 3600_000);
  const day = baku.getUTCDay() || 7;
  const monday = new Date(Date.UTC(baku.getUTCFullYear(), baku.getUTCMonth(), baku.getUTCDate() - day + 1 - 7));
  return Array.from({ length: n }, (_, i) => new Date(monday.getTime() - i * 7 * 86_400_000).toISOString().slice(0, 10));
}

export default async function GeneratePage() {
  let unavailable: string | null = null;
  try {
    await ensureSchema();
  } catch {
    unavailable = "The application database could not be reached, so no report can be requested right now.";
  }
  if (unavailable) {
    return (
      <>
        <header className="topbar"><h1>Generate a report</h1></header>
        <div className="content"><Empty title={unavailable} /></div>
      </>
    );
  }
  const env = environment();
  const [who, availability, lastCheck, jobs, archived] = await Promise.all([
    viewer(),
    meta<Availability>("data_availability"),
    meta<SourceCheck>("last_source_check"),
    recentJobs(env, 20),
    sql()<{ report_type: string; edition: string; sector: string | null }[]>`
      SELECT DISTINCT report_type, edition, sector FROM publication_records
       WHERE report_type IN ('monthly','sector') ORDER BY edition DESC LIMIT 60`,
  ]);
  const d = dispatcher(env);
  const active = jobs.filter((j) => ["queued", "dispatched", "running"].includes(j.status));
  const anchors = availability?.monthly?.anchors ?? {};

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Generate a report</h1>
          <p>
            A request runs the reporting pipeline on a background runner. You can close this page:
            the job keeps going, and its page shows where it is and what came of it.
          </p>
        </div>
      </header>

      <div className="content">
        {!("dispatch" in d) && (
          <div className="notice">
            <strong>No runner will be started from this deployment.</strong>
            <div style={{ marginTop: 6 }}>{d.reason}. Requests are recorded and will start once dispatch is configured.</div>
          </div>
        )}

        <div className="grid grid-2" style={{ alignItems: "start" }}>
          <Card title="What to produce">
            <GenerateForm
              sectors={[...SECTORS]}
              latest={{
                month: availability?.monthly?.edition_month ?? null,
                week: availability?.weekly?.latest_complete_week ?? null,
              }}
              weeks={previousMondays(8)}
              archivedMonths={[...new Set(archived.filter((a) => a.report_type === "monthly").map((a) => a.edition))]}
              publications={availability?.publications ?? {}}
              canEmail={Boolean(who?.email)}
              email={who?.email ?? null}
              admin={Boolean(who?.admin)}
            />
          </Card>

          <div className="stack">
            <Card title="Data available now" note={availability
              ? `As of the last run, ${sinceNow(availability.computed_at)}.` : "Not yet recorded by a run."}>
              {availability ? (
                <table className="data">
                  <tbody>
                    <tr><td>Monthly Monitor would cover</td><td className="num">{availability.monthly.edition_month ?? "—"}</td></tr>
                    {Object.entries(anchors).map(([role, a]) => (
                      <tr key={role}>
                        <td>{role === "banking" ? "Banking tables" : role === "macro" ? "Macro headline" : role === "prices" ? "Consumer prices" : role}
                          <span className="sub">{a.datasets.join(", ")}</span></td>
                        <td className="num">{a.period_end ?? "—"}</td>
                      </tr>
                    ))}
                    <tr><td>Weekly Digest would cover</td>
                      <td className="num">{availability.weekly.latest_complete_week.start} to {availability.weekly.latest_complete_week.end}</td></tr>
                  </tbody>
                </table>
              ) : (
                <p className="muted" style={{ fontSize: 13 }}>No worker has reported the dataset's periods yet.</p>
              )}
              <div className="stat-prov">
                Monthly banking data and macro data may cover different periods: the banking tables for a
                month appear weeks after it ends, prices sooner, national accounts quarterly. Every edition
                states the period of each input it uses.
              </div>
            </Card>

            <Card title="Last source check">
              {lastCheck ? (
                <div style={{ fontSize: 13.5 }}>
                  <div>{dateLong(lastCheck.at)} ({sinceNow(lastCheck.at)})</div>
                  <div className="prov-line">
                    {lastCheck.datasets_checked} datasets checked
                    {lastCheck.failed_datasets.length ? `, ${lastCheck.failed_datasets.length} could not be read` : ""}.
                    {" "}Changes: {Object.keys(lastCheck.changes).length
                      ? Object.entries(lastCheck.changes).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ")
                      : "none"}.
                  </div>
                  <div className="prov-line"><Link href={`/jobs/${lastCheck.job_id}`}>Open that check</Link></div>
                </div>
              ) : (
                <p className="muted" style={{ fontSize: 13 }}>No source check has completed yet.</p>
              )}
            </Card>

            <Card title="In progress">
              {active.length === 0 ? (
                <p className="muted" style={{ fontSize: 13 }}>Nothing is running.</p>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13.5 }}>
                  {active.map((j) => (
                    <li key={j.job_id}>
                      <Link href={`/jobs/${j.job_id}`}>
                        {j.kind === "source_check" ? "Source check" : describe(j.report_type ?? "", j.params)}
                      </Link>{" "}
                      <StatusBadge status={j.status} /> <span className="muted">{j.stage}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        </div>
      </div>
    </>
  );
}
