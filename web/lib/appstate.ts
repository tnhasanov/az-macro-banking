/**
 * Application state: the web application's side of the jobs, publications and notifications that
 * azmonitor/appstate defines.
 *
 * The rules that must hold for both sides live in the database, not here: what makes two requests
 * equivalent (`azm_equivalence_key`), that only one equivalent job is active at a time (a partial
 * unique index), that one scheduled slot starts one check, that an edition is announced to a
 * recipient once. This module writes rows and lets Postgres refuse the ones that break a rule, so a
 * double-click, two tabs or two scheduler invocations cannot talk their way past it.
 */
import { sql } from "./db";
import { newId } from "./ids";
import { ADVISORY_LOCK_KEY, BOOTSTRAP, LATEST, MIGRATIONS } from "./generated/appstate-schema";
import type { Params } from "./params";

export type Env = "production" | "preview" | "development" | "test";

/** Which deployment this is. Rows carry it, so a preview can never act on production's jobs. */
export function environment(): Env {
  const v = (process.env.AZMONITOR_ENVIRONMENT || process.env.VERCEL_ENV || "development").trim();
  return (["production", "preview", "development", "test"] as const).includes(v as Env)
    ? (v as Env) : "development";
}

// ------------------------------------------------------------------ schema

let ensured: Promise<number> | null = null;

/**
 * Apply any migration this database has not had, under the same advisory lock the worker uses.
 * Additive only: every migration is `CREATE ... IF NOT EXISTS` or `ADD COLUMN IF NOT EXISTS`, and
 * nothing here drops or recreates a table.
 */
export function ensureSchema(): Promise<number> {
  ensured ??= (async () => {
    const [{ present }] = await sql()<{ present: string | null }[]>`
      SELECT to_regclass('schema_migrations')::text AS present`;
    if (present) {
      const [{ v }] = await sql()<{ v: number }[]>`SELECT coalesce(max(version), 0)::int AS v FROM schema_migrations`;
      if (v >= LATEST) return v;
    }
    await sql().begin(async (tx) => {
      await tx`SELECT pg_advisory_xact_lock(${ADVISORY_LOCK_KEY}::bigint)`;
      await tx.unsafe(BOOTSTRAP);
      const done = new Set((await tx<{ version: number }[]>`SELECT version FROM schema_migrations`)
        .map((r) => r.version));
      for (const m of MIGRATIONS) {
        if (done.has(m.version)) continue;
        await tx.unsafe(m.sql);
        await tx`INSERT INTO schema_migrations(version, name, applied_by) VALUES (${m.version}, ${m.name}, 'web')`;
      }
    });
    return LATEST;
  })().catch((error) => {
    ensured = null;
    throw error;
  });
  return ensured;
}

// ------------------------------------------------------------------ jobs

export const ACTIVE = ["queued", "dispatched", "running"] as const;
export const TERMINAL = ["succeeded", "reused", "unchanged", "waiting_for_data", "blocked", "failed",
  "cancelled"] as const;

export interface JobRow {
  job_id: string;
  kind: string;
  report_type: string | null;
  params: Params & Record<string, unknown>;
  trigger: string;
  environment: string;
  parent_job_id: string | null;
  schedule_slot: string | null;
  requested_by: string;
  requester_email: string | null;
  notify_requester: boolean;
  force_reason: string | null;
  status: string;
  stage: string | null;
  stage_detail: string | null;
  stage_started_at: string | null;
  attempt: number;
  max_attempts: number;
  worker_id: string | null;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  run_id: number | null;
  run_attempt: number | null;
  run_url: string | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  next_attempt_at: string | null;
  result: Record<string, unknown>;
  edition_id: string | null;
  error_code: string | null;
  error_message: string | null;
  cancel_requested: boolean;
}

export interface CreateJob {
  kind: "report" | "source_check" | "weekly_digest";
  reportType: string | null;
  params: Record<string, unknown>;
  trigger: "manual" | "schedule" | "retry" | "admin_force";
  environment: Env;
  requestedBy: string;
  requesterEmail?: string | null;
  notifyRequester?: boolean;
  forceReason?: string | null;
  nonce?: string | null;
  scheduleSlot?: string | null;
  parentJobId?: string | null;
}

/**
 * Create a job, or find the equivalent one already active. The insert and its first dispatch row
 * are one transaction: a job never exists without the intent to start it.
 */
export async function createJob(c: CreateJob): Promise<{ jobId: string; created: boolean }> {
  const jobId = newId("job");
  return sql().begin(async (tx) => {
    const params = tx.json(c.params as never);
    const inserted = await tx<{ job_id: string }[]>`
      INSERT INTO report_jobs(job_id, kind, report_type, params, equivalence_key, trigger, environment,
                              parent_job_id, schedule_slot, requested_by, requester_email, notify_requester,
                              force_reason, status, stage)
      VALUES (${jobId}, ${c.kind}, ${c.reportType}::text, ${params}::jsonb,
              azm_equivalence_key(${c.kind}::text, ${c.reportType}::text, ${params}::jsonb, ${c.nonce ?? null}::text),
              ${c.trigger}, ${c.environment}, ${c.parentJobId ?? null}::text, ${c.scheduleSlot ?? null}::text,
              ${c.requestedBy}, ${c.requesterEmail ?? null}::text, ${Boolean(c.notifyRequester)},
              ${c.forceReason ?? null}::text, 'queued', 'queued')
      ON CONFLICT DO NOTHING
      RETURNING job_id`;
    if (inserted.length === 0) {
      const existing = await tx<{ job_id: string }[]>`
        SELECT job_id FROM report_jobs
         WHERE environment = ${c.environment}
           AND ((equivalence_key = azm_equivalence_key(${c.kind}::text, ${c.reportType}::text, ${params}::jsonb,
                                                       ${c.nonce ?? null}::text)
                 AND status IN ('queued','dispatched','running'))
                OR (${c.scheduleSlot ?? null}::text IS NOT NULL AND schedule_slot = ${c.scheduleSlot ?? null}::text))
         ORDER BY requested_at DESC LIMIT 1`;
      if (!existing[0]) throw new Error("the job insert conflicted but no equivalent job was found");
      if (c.notifyRequester && c.requesterEmail) {
        // Asking to be emailed about a job someone else already started: the worker reads these
        // fields at publication time, so the request is honoured without a second job.
        await tx`UPDATE report_jobs SET notify_requester = true,
                        requester_email = coalesce(requester_email, ${c.requesterEmail})
                  WHERE job_id = ${existing[0].job_id} AND status IN ('queued','dispatched','running')`;
      }
      return { jobId: existing[0].job_id, created: false };
    }
    await tx`INSERT INTO job_events(job_id, attempt, stage, status, message, detail)
             VALUES (${jobId}, 0, 'queued', 'queued', ${`requested (${c.trigger})`},
                     ${tx.json({ requested_by: c.requestedBy, params: c.params } as never)})`;
    await tx`INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status)
             VALUES (${newId("dsp")}, ${jobId}, 1, 'pending') ON CONFLICT DO NOTHING`;
    if (c.trigger === "admin_force" || c.forceReason) {
      await tx`INSERT INTO audit_log(environment, actor, action, target, reason, detail)
               VALUES (${c.environment}, ${c.requestedBy}, 'force_regenerate', ${jobId}, ${c.forceReason ?? null},
                       ${tx.json({ report_type: c.reportType, params: c.params } as never)})`;
    }
    return { jobId, created: true };
  });
}

export async function getJob(jobId: string): Promise<JobRow | null> {
  const rows = await sql()<JobRow[]>`SELECT * FROM report_jobs WHERE job_id = ${jobId}`;
  return rows[0] ?? null;
}

export async function jobEvents(jobId: string) {
  return sql()<{ at: string; attempt: number; stage: string | null; status: string | null; message: string | null }[]>`
    SELECT at, attempt, stage, status, message FROM job_events WHERE job_id = ${jobId} ORDER BY id`;
}

export async function childJobs(jobId: string) {
  return sql()<Pick<JobRow, "job_id" | "report_type" | "params" | "status" | "stage" | "edition_id"
    | "error_message" | "requested_at">[]>`
    SELECT job_id, report_type, params, status, stage, edition_id, error_message, requested_at
      FROM report_jobs WHERE parent_job_id = ${jobId} ORDER BY requested_at`;
}

export async function recentJobs(env: Env, limit = 50) {
  return sql()<JobRow[]>`
    SELECT * FROM report_jobs WHERE environment = ${env}
     ORDER BY requested_at DESC LIMIT ${limit}`;
}

export async function requestCancel(jobId: string, actor: string): Promise<string | null> {
  const rows = await sql()<{ status: string }[]>`
    UPDATE report_jobs SET cancel_requested = true,
           status = CASE WHEN status IN ('queued') THEN 'cancelled' ELSE status END,
           finished_at = CASE WHEN status IN ('queued') THEN now() ELSE finished_at END,
           error_code = CASE WHEN status IN ('queued') THEN 'cancelled' ELSE error_code END,
           error_message = CASE WHEN status IN ('queued') THEN 'Cancelled before it started.' ELSE error_message END
     WHERE job_id = ${jobId} AND status IN ('queued','dispatched','running')
     RETURNING status`;
  if (rows[0]) {
    await sql()`INSERT INTO job_events(job_id, attempt, status, message)
                SELECT job_id, attempt, status, ${`cancellation requested by ${actor}`} FROM report_jobs
                 WHERE job_id = ${jobId}`;
    await sql()`UPDATE job_dispatches SET status = 'abandoned', last_error = 'cancelled'
                 WHERE job_id = ${jobId} AND status = 'pending'`;
  }
  return rows[0]?.status ?? null;
}

// ------------------------------------------------------------------ publications

export interface Publication {
  edition_id: string;
  report_type: string;
  edition: string;
  version: number;
  sector: string | null;
  environment: string;
  job_id: string | null;
  cause: string;
  published_at: string;
  manifest: {
    prefix: string;
    files: { name: string; key: string; bytes: number; sha256: string; role: string; content_type: string }[];
    n_slides?: number | null;
    as_of?: string | null;
    partial?: boolean;
    publication?: { edition?: string; published_at?: string } | null;
    window?: { start?: string; end?: string } | null;
  };
  validation: { checks?: { id: string; ok: boolean; detail: string }[] };
  reporting_periods: Record<string, string>;
  information_cutoff: string | null;
  findings: string[];
  limitations: string[];
  supersedes: string | null;
}

export async function publication(editionId: string): Promise<Publication | null> {
  const rows = await sql()<Publication[]>`SELECT * FROM publication_records WHERE edition_id = ${editionId}`;
  return rows[0] ?? null;
}

export async function publicationFor(reportType: string, edition: string, version: number) {
  const rows = await sql()<Publication[]>`
    SELECT * FROM publication_records WHERE report_type = ${reportType} AND edition = ${edition}
       AND version = ${version}`;
  return rows[0] ?? null;
}

export async function latestPublications(limit = 30) {
  return sql()<Publication[]>`SELECT * FROM publication_records ORDER BY published_at DESC LIMIT ${limit}`;
}

/**
 * An identical, validated edition that already answers this request, if there is one.
 *
 * "Identical" is decided by the engine, not guessed here: the worker records the fingerprint of
 * every report it evaluates, with the published edition that has it. That edition is still the
 * answer only if no productive source change has been detected since — new observations, a
 * revision or a new publication would change the fingerprint. A request to check the sources
 * first is never answered this way, because its whole point is to look again.
 */
export async function identicalEdition(env: Env, reportType: string, params: Params,
  latestPublicationId?: string | null): Promise<Publication | null> {
  if (params.refresh !== "latest_data") return null;
  if ((reportType === "monthly" || reportType === "sector") && params.period && params.period !== "latest") {
    const edition = reportType === "monthly" ? params.period : `${params.sector}_${params.period}`;
    const rows = await sql()<Publication[]>`
      SELECT * FROM publication_records WHERE report_type = ${reportType} AND edition = ${edition}
       ORDER BY version DESC LIMIT 1`;
    return rows[0] ?? null;
  }
  let scope = params.sector ?? params.publication_id ?? params.period ?? "latest";
  if (scope === "latest" && params.publication_id === "latest" && latestPublicationId) scope = latestPublicationId;
  const rows = await sql()<Publication[]>`
    SELECT p.* FROM current_fingerprints f
      JOIN publication_records p ON p.edition_id = f.edition_id
     WHERE f.environment = ${env} AND f.report_type = ${reportType} AND f.scope_key = ${scope}
       AND NOT EXISTS (SELECT 1 FROM source_changes c
                        WHERE c.classification IN ('new_publication','new_observations','substantive_revision')
                          AND c.detected_at > f.computed_at)
       AND NOT EXISTS (SELECT 1 FROM publication_records newer
                        WHERE newer.report_type = p.report_type AND newer.edition = p.edition
                          AND newer.version > p.version)`;
  return rows[0] ?? null;
}

// ------------------------------------------------------------------ what the worker last saw

export async function meta<T>(key: string): Promise<T | null> {
  try {
    const rows = await sql()<{ value: T }[]>`SELECT value FROM meta WHERE key = ${key}`;
    return rows[0]?.value ?? null;
  } catch {
    return null;           // the read model has not been created yet
  }
}

export interface Availability {
  computed_at: string;
  monthly: { edition_month: string | null; anchors: Record<string, { datasets: string[]; period_end: string | null }>; note: string };
  sector: { edition_month: string | null; sectors: string[] };
  weekly: { latest_complete_week: { start: string; end: string } };
  publications: Record<string, { publication_id: string; edition: string | null; published_at: string | null }[]>;
}

export interface SourceCheck {
  at: string;
  job_id: string;
  changes: Record<string, number>;
  datasets_checked: number;
  failed_datasets: string[];
}

// ------------------------------------------------------------------ settings

export interface NotificationSettings {
  environment: string;
  auto_email_enabled: boolean;
  auto_checks_enabled: boolean;
  paused: boolean;
  paused_reason: string | null;
  notify_revisions: boolean;
  attach_pdf: boolean;
  revision_settle_minutes: number;
  updated_at: string | null;
  updated_by: string | null;
}

export async function notificationSettings(env: Env): Promise<NotificationSettings> {
  const rows = await sql()<NotificationSettings[]>`SELECT * FROM notification_settings WHERE environment = ${env}`;
  return rows[0] ?? {
    environment: env, auto_email_enabled: false, auto_checks_enabled: false, paused: false, paused_reason: null,
    notify_revisions: true, attach_pdf: true, revision_settle_minutes: 120, updated_at: null, updated_by: null,
  };
}

export async function saveSettings(env: Env, actor: string, s: Partial<NotificationSettings>) {
  const current = await notificationSettings(env);
  const next = { ...current, ...s };
  await sql().begin(async (tx) => {
    await tx`
      INSERT INTO notification_settings(environment, auto_email_enabled, auto_checks_enabled, paused, paused_reason,
             notify_revisions, attach_pdf, revision_settle_minutes, updated_at, updated_by)
      VALUES (${env}, ${next.auto_email_enabled}, ${next.auto_checks_enabled}, ${next.paused}, ${next.paused_reason},
              ${next.notify_revisions}, ${next.attach_pdf}, ${next.revision_settle_minutes}, now(), ${actor})
      ON CONFLICT (environment) DO UPDATE SET
        auto_email_enabled = EXCLUDED.auto_email_enabled, auto_checks_enabled = EXCLUDED.auto_checks_enabled,
        paused = EXCLUDED.paused, paused_reason = EXCLUDED.paused_reason,
        notify_revisions = EXCLUDED.notify_revisions, attach_pdf = EXCLUDED.attach_pdf,
        revision_settle_minutes = EXCLUDED.revision_settle_minutes, updated_at = now(), updated_by = EXCLUDED.updated_by`;
    await tx`INSERT INTO audit_log(environment, actor, action, target, detail)
             VALUES (${env}, ${actor}, 'notification_settings', ${env},
                     ${tx.json({ before: current, after: next } as never)})`;
  });
  return next;
}

export interface Recipient {
  recipient_id: string;
  email: string;
  display_name: string | null;
  role: "owner" | "subscriber";
  active: boolean;
  unsubscribed_at: string | null;
  suppressed_at: string | null;
  suppressed_reason: string | null;
  subscriptions: { report_type: string; sector: string }[];
}

export async function recipients(): Promise<Recipient[]> {
  return sql()<Recipient[]>`
    SELECT r.*, coalesce((SELECT jsonb_agg(jsonb_build_object('report_type', s.report_type, 'sector', s.sector)
                                           ORDER BY s.report_type, s.sector)
                            FROM subscriptions s WHERE s.recipient_id = r.recipient_id), '[]'::jsonb) AS subscriptions
      FROM recipients r ORDER BY r.role, r.email`;
}

export async function upsertRecipient(actor: string, input: {
  email: string; displayName?: string | null; role: "owner" | "subscriber"; active: boolean;
  subscriptions: { report_type: string; sector: string }[];
}) {
  return sql().begin(async (tx) => {
    const rows = await tx<{ recipient_id: string }[]>`
      INSERT INTO recipients(recipient_id, email, display_name, role, active, created_by)
      VALUES (${newId("rcp")}, ${input.email}, ${input.displayName ?? null}, ${input.role}, ${input.active}, ${actor})
      ON CONFLICT (lower(email)) DO UPDATE SET display_name = EXCLUDED.display_name, role = EXCLUDED.role,
             active = EXCLUDED.active, updated_at = now(),
             unsubscribed_at = CASE WHEN EXCLUDED.active THEN NULL ELSE recipients.unsubscribed_at END
      RETURNING recipient_id`;
    const id = rows[0].recipient_id;
    await tx`DELETE FROM subscriptions WHERE recipient_id = ${id}`;
    for (const s of input.subscriptions) {
      await tx`INSERT INTO subscriptions(recipient_id, report_type, sector) VALUES (${id}, ${s.report_type}, ${s.sector})
               ON CONFLICT DO NOTHING`;
    }
    await tx`INSERT INTO audit_log(actor, action, target, detail)
             VALUES (${actor}, 'recipient_saved', ${id}, ${tx.json(input as never)})`;
    return id;
  });
}

export async function clearSuppression(actor: string, recipientId: string) {
  await sql()`UPDATE recipients SET suppressed_at = NULL, suppressed_reason = NULL, updated_at = now()
               WHERE recipient_id = ${recipientId}`;
  await sql()`INSERT INTO audit_log(actor, action, target) VALUES (${actor}, 'suppression_cleared', ${recipientId})`;
}

export async function unsubscribe(recipientId: string, how: string) {
  const rows = await sql()<{ email: string }[]>`
    UPDATE recipients SET unsubscribed_at = coalesce(unsubscribed_at, now()), updated_at = now()
     WHERE recipient_id = ${recipientId} RETURNING email`;
  if (rows[0]) {
    await sql()`UPDATE email_outbox SET status = 'cancelled', status_reason = 'recipient unsubscribed', updated_at = now()
                 WHERE recipient_id = ${recipientId} AND status = 'queued'
                   AND purpose IN ('new_edition','revised_edition')`;
    await sql()`INSERT INTO audit_log(actor, action, target, reason) VALUES ('recipient', 'unsubscribed', ${recipientId}, ${how})`;
  }
  return rows[0]?.email ?? null;
}

// ------------------------------------------------------------------ throttling

/**
 * A fixed-window counter in Postgres, shared by every serverless instance. Returns whether this
 * request is within the limit. The per-instance map this replaces slowed an attacker down only on
 * the instance they happened to hit.
 */
export async function withinLimit(bucket: string, limit: number, windowSeconds: number): Promise<boolean> {
  const rows = await sql()<{ hits: number }[]>`
    INSERT INTO request_throttle(bucket, window_start, hits)
    VALUES (${bucket}, to_timestamp(floor(extract(epoch FROM now()) / ${windowSeconds}) * ${windowSeconds}), 1)
    ON CONFLICT (bucket, window_start) DO UPDATE SET hits = request_throttle.hits + 1
    RETURNING hits`;
  if (Math.random() < 0.02) {
    await sql()`DELETE FROM request_throttle WHERE window_start < now() - interval '2 days'`.catch(() => undefined);
  }
  return rows[0].hits <= limit;
}

export async function audit(env: Env, actor: string, action: string, target: string | null,
  reason?: string | null, detail?: Record<string, unknown>) {
  await sql()`INSERT INTO audit_log(environment, actor, action, target, reason, detail)
              VALUES (${env}, ${actor}, ${action}, ${target}, ${reason ?? null}, ${sql().json((detail ?? {}) as never)})`;
}
