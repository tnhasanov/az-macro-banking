/**
 * Asking for a report.
 *
 * POST returns promptly with a durable job id; the work happens on a runner and the browser can be
 * closed. Three answers are possible, in this order:
 *
 *   - **an existing edition**, when one with identical inputs is already published and the request
 *     did not ask to check the sources first (the person can still choose to generate anyway, which
 *     runs the pipeline and lets the engine confirm it);
 *   - **an existing job**, when an equivalent one is already queued or running — two clicks, two
 *     tabs or the scheduler asking for the same report share one job;
 *   - **a new job**, dispatched immediately.
 *
 * Nothing in the body chooses a command, a repository, a branch, a URL or an email recipient.
 */
import { NextResponse } from "next/server";
import { createJob, environment, identicalEdition, meta, recentJobs, type Availability } from "@/lib/appstate";
import { drainDispatches } from "@/lib/dispatch";
import { handler, jsonBody, Refusal, throttle } from "@/lib/guard";
import { InvalidRequest, isReportType, normalise, SECTORS } from "@/lib/params";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface Body {
  report_type?: unknown;
  params?: Record<string, unknown>;
  email_me?: unknown;
  generate_anyway?: unknown;
  force_reason?: unknown;
}

function bakuToday(): string {
  return new Date(Date.now() + 4 * 3600_000).toISOString().slice(0, 10);
}

/** Refusals that need no dataset: a period in the future, or a week that has not ended. */
function obviouslyUnavailable(reportType: string, period: string | undefined): string | null {
  if (!period || period === "latest") return null;
  const today = bakuToday();
  if ((reportType === "monthly" || reportType === "sector") && period >= today.slice(0, 7)) {
    return `${period} has not ended yet, so no banking data for it can have been published.`;
  }
  if (reportType === "weekly") {
    const end = new Date(`${period}T00:00:00Z`);
    end.setUTCDate(end.getUTCDate() + 6);
    if (end.toISOString().slice(0, 10) >= today) {
      return `The week of ${period} has not ended yet; a digest covers a complete Monday-to-Sunday week.`;
    }
  }
  return null;
}

export const POST = handler(async (request, caller) => {
  await throttle(caller, "create_job", 30, 3600);
  const body = await jsonBody<Body>(request);
  const reportType = String(body.report_type ?? "");
  if (!isReportType(reportType)) throw new Refusal(400, "Choose a report type.", "unknown_report");
  let params;
  try {
    params = normalise(reportType, body.params, SECTORS);
  } catch (error) {
    if (error instanceof InvalidRequest) throw new Refusal(400, error.message, error.code);
    throw error;
  }
  const unavailable = obviouslyUnavailable(reportType, params.period);
  if (unavailable) throw new Refusal(422, unavailable, "unsupported_period");

  const emailMe = body.email_me === true;
  if (emailMe && !caller.email) {
    throw new Refusal(400, "This sign-in has no email address (AZMONITOR_OWNER_EMAIL is not set), so it cannot "
      + "be emailed.", "no_email");
  }
  const forceReason = typeof body.force_reason === "string" ? body.force_reason.trim() : "";
  if (forceReason && !caller.admin) throw new Refusal(403, "Only an administrator can force a regeneration.", "forbidden");
  if (body.force_reason !== undefined && forceReason.length < 10) {
    throw new Refusal(400, "Say why the report must be regenerated (at least ten characters); it is recorded.",
      "reason_required");
  }
  if (forceReason.length > 500) throw new Refusal(400, "Keep the reason under 500 characters.", "too_long");

  const env = environment();
  if (!forceReason && body.generate_anyway !== true) {
    const availability = await meta<Availability>("data_availability");
    const latestPub = availability?.publications?.[reportType]?.[0]?.publication_id ?? null;
    const offer = await identicalEdition(env, reportType, params, latestPub);
    if (offer) {
      return NextResponse.json({
        offer: {
          edition_id: offer.edition_id, report_type: offer.report_type, edition: offer.edition,
          version: offer.version, published_at: offer.published_at,
          href: `/reports/${offer.report_type}/${encodeURIComponent(offer.edition)}/${offer.version}`,
          files: (offer.manifest.files ?? []).filter((f) => ["pdf", "pptx", "xlsx"].includes(f.role))
            .map((f) => ({ name: f.name, role: f.role, bytes: f.bytes, href: `/api/files/${f.key}` })),
        },
        note: "An edition with identical inputs is already published. Open it, have it emailed to you, "
          + "or generate anyway to have the engine check again.",
      });
    }
  }

  const { jobId, created } = await createJob({
    kind: "report", reportType, params: params as unknown as Record<string, unknown>,
    trigger: forceReason ? "admin_force" : "manual", environment: env, requestedBy: caller.subject,
    requesterEmail: emailMe ? caller.email : null, notifyRequester: emailMe,
    forceReason: forceReason || null, nonce: forceReason ? `force:${forceReason}` : null,
  });
  let dispatch: unknown = null;
  if (created) {
    // Start it now rather than at the next tick. A failure here is not the request's failure: the
    // dispatch row is durable and the tick retries it.
    dispatch = await drainDispatches(env, { jobId }).catch((e) => ({ error: e instanceof Error ? e.message : String(e) }));
  }
  return NextResponse.json({ job_id: jobId, created, href: `/jobs/${jobId}`,
    note: created ? "Job created." : "An identical job is already in progress; you have been attached to it.",
    dispatch: summariseDispatch(dispatch) }, { status: created ? 201 : 200 });
}, { write: true });

function summariseDispatch(d: unknown): string | null {
  if (!d || typeof d !== "object") return null;
  const r = d as { dispatcher?: string; reason?: string; results?: { outcome: string; message?: string }[] };
  if (r.dispatcher === "off") return `No runner was started: ${r.reason}.`;
  const first = r.results?.[0];
  return first ? `${first.outcome}${first.message ? `: ${first.message}` : ""}` : null;
}

export const GET = handler(async () => {
  const jobs = await recentJobs(environment(), 50);
  return NextResponse.json({
    jobs: jobs.map((j) => ({
      job_id: j.job_id, kind: j.kind, report_type: j.report_type, params: j.params, trigger: j.trigger,
      status: j.status, stage: j.stage, requested_at: j.requested_at, finished_at: j.finished_at,
      edition_id: j.edition_id, error_message: j.error_message, parent_job_id: j.parent_job_id,
    })),
  });
});
