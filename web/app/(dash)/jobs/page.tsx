/** Every job in this environment, newest first: requested, scheduled, and the reports checks produced. */
import Link from "next/link";
import { ensureSchema, environment, recentJobs } from "@/lib/appstate";
import { Card, Empty, StatusBadge } from "@/components/ui";
import { dateLong } from "@/lib/format";
import { describe } from "@/lib/params";

export const dynamic = "force-dynamic";

export default async function JobsPage() {
  await ensureSchema();
  const jobs = await recentJobs(environment(), 100);
  return (
    <>
      <header className="topbar">
        <div>
          <h1>Jobs</h1>
          <p>Requested reports, scheduled source checks, and the reports those checks produced.</p>
        </div>
        <Link className="button" href="/generate">Generate a report</Link>
      </header>
      <div className="content">
        {jobs.length === 0 ? <Empty title="No jobs yet." /> : (
          <Card>
            <div className="table-scroll">
              <table className="data">
                <thead><tr><th>Job</th><th>Trigger</th><th>Status</th><th>Requested</th><th>Result</th></tr></thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.job_id}>
                      <td>
                        <Link href={`/jobs/${j.job_id}`}>
                          {j.kind === "source_check" ? "Source check" : describe(j.report_type ?? "", j.params ?? {})}
                        </Link>
                        <span className="sub mono">{j.job_id}{j.parent_job_id ? " · from a source check" : ""}</span>
                      </td>
                      <td>{j.trigger.replace("_", " ")}</td>
                      <td><StatusBadge status={j.status} />{j.status === "running" && j.stage && <span className="sub">{j.stage.replace(/_/g, " ")}</span>}</td>
                      <td className="nowrap">{dateLong(j.requested_at)}</td>
                      <td style={{ maxWidth: 360 }}>{j.edition_id ?? j.error_message ?? (j.result?.message as string | undefined) ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </div>
    </>
  );
}
