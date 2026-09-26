/**
 * One job as a person sees it: what was asked, where it is, what came of it.
 *
 * Built from an allow-list. The job row also holds the worker's error detail (a traceback) and the
 * events' raw detail; those stay in the database and the run log. The browser gets the concise
 * message, the stage, the timings and links to the application's own pages and download route.
 */
import { childJobs, getJob, jobEvents, publication, type JobRow } from "./appstate";
import { deliveriesFor } from "./email/outbox";
import { describe } from "./params";

export const DISPLAY_STAGES = [
  { id: "queued", label: "Queued", covers: ["queued", "starting", "waiting_for_dataset", "restoring"] },
  { id: "collecting", label: "Collecting", covers: ["collecting"] },
  { id: "calculating", label: "Calculating", covers: ["calculating"] },
  { id: "writing_narrative", label: "Writing narrative", covers: ["writing_narrative"] },
  { id: "rendering", label: "Rendering", covers: ["rendering"] },
  { id: "validating", label: "Validating", covers: ["validating"] },
  { id: "uploading", label: "Uploading", covers: ["uploading", "publishing"] },
  { id: "complete", label: "Complete", covers: ["complete"] },
] as const;

const SUB_STAGE: Record<string, string> = {
  starting: "a runner has started", waiting_for_dataset: "waiting for another job to finish with the dataset",
  restoring: "restoring the dataset from private storage", publishing: "recording the publication",
};

const RESULT_KEYS = ["message", "waiting", "items", "changes", "latest_available", "requester_email_queued",
  "failed_checks", "checks", "cause", "supersedes", "notifications", "reporting_periods", "plan"];

function mask(address: string): string {
  const [user, domain] = address.split("@");
  if (!domain) return address;
  return `${user.slice(0, 2)}${"•".repeat(Math.max(1, user.length - 2))}@${domain}`;
}

export async function jobView(jobId: string, viewerEmail: string | null) {
  const job = await getJob(jobId);
  if (!job) return null;
  const [events, children, emails] = await Promise.all([
    jobEvents(jobId), childJobs(jobId), deliveriesFor({ jobId }),
  ]);
  const edition = job.edition_id ? await publication(job.edition_id) : null;
  const passed = new Set(events.map((e) => e.stage).filter(Boolean) as string[]);
  const collects = job.kind === "source_check" || job.params?.refresh === "check_sources";
  const stages = DISPLAY_STAGES.map((s) => {
    const current = job.status === "running" || job.status === "dispatched" || job.status === "queued"
      ? s.covers.includes((job.stage ?? "queued") as never) : false;
    const reached = s.covers.some((c) => passed.has(c)) || (s.id === "complete" && job.stage === "complete");
    return {
      id: s.id, label: s.label,
      state: s.id === "collecting" && !collects ? "skipped"
        : current ? "current" : reached ? "done" : "pending",
    };
  });
  const heartbeatAge = job.heartbeat_at ? (Date.now() - new Date(job.heartbeat_at).getTime()) / 1000 : null;
  const result: Record<string, unknown> = {};
  for (const k of RESULT_KEYS) if (job.result && k in job.result) result[k] = job.result[k];

  return {
    job: {
      job_id: job.job_id, kind: job.kind, report_type: job.report_type, params: job.params,
      description: job.kind === "source_check" ? "Source check" : describe(job.report_type ?? "", job.params ?? {}),
      trigger: job.trigger, environment: job.environment, status: job.status, stage: job.stage,
      stage_label: job.stage && SUB_STAGE[job.stage] ? SUB_STAGE[job.stage] : null,
      stage_detail: job.stage_detail, stage_started_at: job.stage_started_at, heartbeat_at: job.heartbeat_at,
      heartbeat_age_seconds: heartbeatAge, stale: job.status === "running" && heartbeatAge !== null && heartbeatAge > 120,
      requested_at: job.requested_at, started_at: job.started_at, finished_at: job.finished_at,
      attempt: job.attempt, max_attempts: job.max_attempts, next_attempt_at: job.next_attempt_at,
      run_url: job.run_url, parent_job_id: job.parent_job_id, requested_by: job.requested_by,
      force_reason: job.force_reason, cancel_requested: job.cancel_requested,
      notify_requester: job.notify_requester,
      error_code: job.error_code, error_message: job.error_message, edition_id: job.edition_id, result,
      terminal: ["succeeded", "reused", "unchanged", "waiting_for_data", "blocked", "failed", "cancelled"].includes(job.status),
    },
    stages,
    events: events.map((e) => ({ at: e.at, attempt: e.attempt, stage: e.stage, status: e.status, message: e.message })),
    children: children.map((c) => ({
      job_id: c.job_id, description: describe(c.report_type ?? "", c.params ?? {}), status: c.status,
      stage: c.stage, edition_id: c.edition_id, error_message: c.error_message,
    })),
    edition: edition && {
      edition_id: edition.edition_id, report_type: edition.report_type, edition: edition.edition,
      version: edition.version, published_at: edition.published_at, cause: edition.cause,
      supersedes: edition.supersedes,
      href: `/reports/${edition.report_type}/${encodeURIComponent(edition.edition)}/${edition.version}`,
      files: (edition.manifest.files ?? []).filter((f) => ["pdf", "pptx", "xlsx"].includes(f.role))
        .map((f) => ({ name: f.name, role: f.role, bytes: f.bytes, href: `/api/files/${f.key}` })),
      findings: edition.findings, limitations: edition.limitations,
      reporting_periods: edition.reporting_periods, information_cutoff: edition.information_cutoff,
      checks: (edition.validation?.checks ?? []).map((c) => ({ id: c.id, ok: c.ok, detail: c.detail })),
    },
    emails: emails.map((e) => ({
      delivery_id: e.delivery_id, purpose: e.purpose, status: e.status, status_reason: e.status_reason,
      to: viewerEmail && e.to_address.toLowerCase() === viewerEmail.toLowerCase() ? e.to_address : mask(e.to_address),
      attempts: e.attempts, accepted_at: e.accepted_at, delivered_at: e.delivered_at,
      last_error: e.last_error, last_event: e.last_event,
    })),
  };
}

export type JobView = NonNullable<Awaited<ReturnType<typeof jobView>>>;
export type { JobRow };
