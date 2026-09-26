/**
 * One job. The address is the job id, so a reload, a bookmark or coming back tomorrow shows the
 * same job; the page then follows it until it reaches an outcome.
 */
import Link from "next/link";
import { notFound } from "next/navigation";
import { ensureSchema } from "@/lib/appstate";
import { JOB_ID_PATTERN } from "@/lib/ids";
import { jobView } from "@/lib/jobview";
import { viewer } from "@/lib/viewer";
import { JobProgress } from "@/components/JobProgress";

export const dynamic = "force-dynamic";

export default async function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) notFound();
  await ensureSchema();
  const who = await viewer();
  const view = await jobView(id, who?.email ?? null);
  if (!view) notFound();
  return (
    <>
      <header className="topbar">
        <div>
          <div className="prov-line"><Link href="/jobs">← All jobs</Link></div>
          <h1>{view.job.description}</h1>
          <p className="mono" style={{ fontSize: 12.5 }}>{view.job.job_id}</p>
        </div>
      </header>
      <div className="content">
        <JobProgress initial={JSON.parse(JSON.stringify(view))} canEmail={Boolean(who?.email)} />
      </div>
    </>
  );
}
