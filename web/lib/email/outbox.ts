/**
 * The delivery ledger's sender: takes queued messages out of `email_outbox`, sends each once, and
 * records exactly what the provider said.
 *
 * Four rules decide everything here:
 *
 *  1. **One message, one key.** A row's delivery id is its idempotency key, and its content is
 *     composed at the first attempt and then frozen, so every retry sends identical bytes under the
 *     same key. Resend returns the original response for a repeated key and payload for 24 hours,
 *     which is what makes retrying an ambiguous send safe inside that window.
 *  2. **Accepted is not delivered.** The provider accepting a message moves it to `accepted`; only
 *     a signed delivery webhook moves it to `delivered`. A bounce or a failure from the webhook
 *     overrides either, and an older event never overrides a newer one.
 *  3. **Uncertain is its own state.** A send that timed out may have gone. Inside the idempotency
 *     window it is retried under the same key; outside it, the row stops at `uncertain` and waits
 *     for a person, because sending again then could send twice.
 *  4. **Recipients' choices win.** An unsubscribed, suppressed or paused recipient is checked at send
 *     time, not only when the row was queued.
 */
import { createHash } from "node:crypto";
import { sql } from "../db";
import { newId } from "../ids";
import { environment, notificationSettings, publication, type Env, type Publication } from "../appstate";
import { readPrivate } from "../blob";
import { sign } from "../signing";
import { compose, composeTest, type ChangeNote } from "./compose";
import { emailProvider, mayDeliver, type EmailProvider, type SendResult } from "./provider";

export const ANNOUNCEMENTS = ["new_edition", "revised_edition"] as const;
const BACKOFF = [60, 300, 1800, 7200, 21600];
/** Resend keeps an idempotency key for 24 hours; stay well inside it. */
const IDEMPOTENCY_WINDOW_HOURS = 23;
export const ATTACHMENT_LIMIT = Number(process.env.AZMONITOR_ATTACHMENT_LIMIT_BYTES ?? 10 * 1024 * 1024);

export interface OutboxRow {
  delivery_id: string;
  environment: string;
  edition_id: string | null;
  job_id: string | null;
  recipient_id: string | null;
  to_address: string;
  purpose: string;
  status: string;
  status_reason: string | null;
  subject: string | null;
  body_text: string | null;
  body_html: string | null;
  attachment_key: string | null;
  attachment_name: string | null;
  attachment_bytes: number | null;
  content_hash: string | null;
  headers: Record<string, string> | null;
  idempotency_key: string;
  attempts: number;
  max_attempts: number;
  not_before: string;
  first_attempt_at: string | null;
  last_attempt_ambiguous: boolean;
  provider: string | null;
  provider_message_id: string | null;
  accepted_at: string | null;
  delivered_at: string | null;
  failed_at: string | null;
  last_event: string | null;
  last_event_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export function baseUrl(): string {
  const configured = process.env.AZMONITOR_APP_URL;
  if (configured) return configured.replace(/\/+$/, "");
  const host = process.env.VERCEL_PROJECT_PRODUCTION_URL ?? process.env.VERCEL_URL;
  return host ? `https://${host}` : "http://localhost:3000";
}

// ------------------------------------------------------------------ queueing

/** "Send this existing report to me": to the signed-in person, and only to them. */
export async function queueCopy(env: Env, editionId: string, to: string, purpose = "manual_resend",
  jobId: string | null = null): Promise<string> {
  const id = newId("dlv");
  await sql()`INSERT INTO email_outbox(delivery_id, environment, edition_id, job_id, to_address, purpose, status,
                                       idempotency_key)
              VALUES (${id}, ${env}, ${editionId}, ${jobId}, ${to}, ${purpose}, 'queued', ${id})`;
  return id;
}

export async function queueTest(env: Env, to: string): Promise<string> {
  const id = newId("dlv");
  await sql()`INSERT INTO email_outbox(delivery_id, environment, to_address, purpose, status, idempotency_key)
              VALUES (${id}, ${env}, ${to}, 'test', 'queued', ${id})`;
  return id;
}

export async function queueAlert(env: Env, to: string, subject: string, text: string): Promise<string> {
  const id = newId("dlv");
  await sql()`INSERT INTO email_outbox(delivery_id, environment, to_address, purpose, status, idempotency_key,
                                       subject, body_text, body_html, content_hash)
              VALUES (${id}, ${env}, ${to}, 'alert', 'queued', ${id}, ${subject}, ${text},
                      ${`<pre style="font-family:Arial,sans-serif;white-space:pre-wrap">${text.replace(/</g, "&lt;")}</pre>`},
                      ${createHash("sha256").update(subject + text).digest("hex")})`;
  return id;
}

// ------------------------------------------------------------------ sending

async function changesFor(p: Publication): Promise<ChangeNote[]> {
  if (!p.job_id) return [];
  return sql()<ChangeNote[]>`
    SELECT classification, dataset_id, publication_id, periods, published_at::text AS published_at
      FROM source_changes WHERE handled_by_job = ${p.job_id} ORDER BY detected_at LIMIT 10`;
}

/** Compose once and freeze. A retry sends exactly what the first attempt sent. */
async function freeze(row: OutboxRow, attachPdf: boolean): Promise<OutboxRow> {
  if (row.subject && row.body_text) return row;
  const base = baseUrl();
  let composed;
  let attachmentKey: string | null = null;
  let attachmentName: string | null = null;
  let attachmentBytes: number | null = null;
  let attachmentHash = "";
  if (row.purpose === "test") {
    composed = composeTest(base);
  } else {
    const p = row.edition_id ? await publication(row.edition_id) : null;
    if (!p) throw new Error(`delivery ${row.delivery_id} names edition ${row.edition_id}, which is not published`);
    const pdf = (p.manifest.files ?? []).find((f) => f.role === "pdf");
    let note: string | null = null;
    if (pdf && attachPdf && pdf.bytes <= ATTACHMENT_LIMIT) {
      attachmentKey = pdf.key;
      attachmentName = pdf.name;
      attachmentBytes = pdf.bytes;
      attachmentHash = pdf.sha256;
    } else if (pdf && attachPdf) {
      note = `The PDF is ${(pdf.bytes / 1048576).toFixed(1)} MB, over the attachment limit, so it is linked rather than attached.`;
    }
    const unsubscribe = row.recipient_id && (ANNOUNCEMENTS as readonly string[]).includes(row.purpose)
      ? `${base}/unsubscribe?t=${encodeURIComponent(sign("unsubscribe", row.recipient_id))}` : null;
    composed = compose(row.purpose, p, {
      baseUrl: base, changes: await changesFor(p), attached: attachmentName, attachmentNote: note,
      unsubscribeUrl: unsubscribe,
    });
    if (unsubscribe) {
      composed.headers["List-Unsubscribe"] =
        `<${base}/api/unsubscribe?t=${encodeURIComponent(sign("unsubscribe", row.recipient_id!))}>`;
    }
  }
  const hash = createHash("sha256")
    .update([composed.subject, composed.text, composed.html, JSON.stringify(composed.headers), attachmentHash].join("\u0000"))
    .digest("hex");
  const rows = await sql()<OutboxRow[]>`
    UPDATE email_outbox SET subject = ${composed.subject}, body_text = ${composed.text}, body_html = ${composed.html},
           attachment_key = ${attachmentKey}, attachment_name = ${attachmentName}, attachment_bytes = ${attachmentBytes},
           content_hash = ${hash}, headers = ${sql().json(composed.headers as never)}, updated_at = now()
     WHERE delivery_id = ${row.delivery_id} AND subject IS NULL
    RETURNING *`;
  return rows[0] ?? (await sql()<OutboxRow[]>`SELECT * FROM email_outbox WHERE delivery_id = ${row.delivery_id}`)[0];
}

function headersOf(row: OutboxRow): Record<string, string> {
  return row.headers && typeof row.headers === "object" ? row.headers : {};
}

async function recordResult(row: OutboxRow, provider: EmailProvider, res: SendResult): Promise<string> {
  if (res.kind === "accepted") {
    await sql()`UPDATE email_outbox SET status = CASE WHEN status = 'sending' THEN 'accepted' ELSE status END,
                       provider = ${provider.name}, provider_message_id = ${res.providerMessageId},
                       accepted_at = coalesce(accepted_at, now()), claimed_until = NULL, last_error = NULL,
                       status_reason = NULL, updated_at = now()
                 WHERE delivery_id = ${row.delivery_id}`;
    await applyPendingEvents(provider.name, res.providerMessageId);
    return "accepted";
  }
  const ageHours = row.first_attempt_at ? (Date.now() - new Date(row.first_attempt_at).getTime()) / 3.6e6 : 0;
  if (res.kind === "uncertain") {
    if (ageHours < IDEMPOTENCY_WINDOW_HOURS && row.attempts < row.max_attempts) {
      const delay = BACKOFF[Math.min(row.attempts - 1, BACKOFF.length - 1)];
      await sql()`UPDATE email_outbox SET status = 'queued', last_attempt_ambiguous = true, claimed_until = NULL,
                         not_before = now() + make_interval(secs => ${delay}), last_error = ${res.message}, updated_at = now()
                   WHERE delivery_id = ${row.delivery_id}`;
      return "retry_uncertain";
    }
    await sql()`UPDATE email_outbox SET status = 'uncertain', last_attempt_ambiguous = true, claimed_until = NULL,
                       last_error = ${`${res.message}. It may or may not have been sent; sending again could `
                       + "send it twice, so it waits for a person to resolve."}, updated_at = now()
                 WHERE delivery_id = ${row.delivery_id}`;
    return "uncertain";
  }
  if (!res.permanent && row.attempts < row.max_attempts) {
    const delay = Math.max(res.retryAfterSeconds ?? 0, BACKOFF[Math.min(row.attempts - 1, BACKOFF.length - 1)]);
    await sql()`UPDATE email_outbox SET status = 'queued', claimed_until = NULL,
                       not_before = now() + make_interval(secs => ${delay}),
                       last_error = ${`${res.code}: ${res.message}`}, updated_at = now()
                 WHERE delivery_id = ${row.delivery_id}`;
    return "retry";
  }
  await sql()`UPDATE email_outbox SET status = 'failed', failed_at = now(), claimed_until = NULL,
                     last_error = ${`${res.code}: ${res.message}`}, updated_at = now()
               WHERE delivery_id = ${row.delivery_id}`;
  return "failed";
}

/**
 * Send what is due. Safe to run from any number of places at once: rows are claimed with SKIP
 * LOCKED and a row whose sender died is picked up again, marked as a possibly-repeated attempt.
 */
export async function drainOutbox(env: Env, opts: { deliveryId?: string; limit?: number; provider?: EmailProvider } = {}) {
  const provider = opts.provider ?? emailProvider();
  const settings = await notificationSettings(env);
  await sql()`UPDATE email_outbox SET status = 'queued', last_attempt_ambiguous = true, claimed_until = NULL,
                     last_error = 'the previous attempt did not report back', updated_at = now()
               WHERE status = 'sending' AND claimed_until < now()`;
  const only = opts.deliveryId ?? null;
  const rows = await sql()<OutboxRow[]>`
    UPDATE email_outbox o SET status = 'sending', claimed_until = now() + interval '2 minutes',
           claimed_by = ${`web/${process.pid}`}, attempts = o.attempts + 1,
           first_attempt_at = coalesce(o.first_attempt_at, now()), updated_at = now()
     WHERE o.delivery_id IN (
       SELECT delivery_id FROM email_outbox
        WHERE environment = ${env} AND status = 'queued' AND not_before <= now()
          AND (${only}::text IS NULL OR delivery_id = ${only}::text)
          AND (purpose NOT IN ('new_edition','revised_edition') OR NOT ${settings.paused})
        ORDER BY created_at LIMIT ${opts.limit ?? 10}
        FOR UPDATE SKIP LOCKED)
    RETURNING o.*`;

  const outcomes: { delivery_id: string; outcome: string }[] = [];
  for (const claimed of rows) {
    const outcome = await sendOne(claimed, provider, settings.attach_pdf).catch(async (error) => {
      const message = error instanceof Error ? error.message : String(error);
      await sql()`UPDATE email_outbox SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                         failed_at = CASE WHEN attempts >= max_attempts THEN now() END,
                         not_before = now() + interval '10 minutes', claimed_until = NULL,
                         last_error = ${message.slice(0, 500)}, updated_at = now()
                   WHERE delivery_id = ${claimed.delivery_id}`;
      return "error";
    });
    outcomes.push({ delivery_id: claimed.delivery_id, outcome });
  }
  return { provider: provider.name, claimed: rows.length, outcomes };
}

async function sendOne(claimed: OutboxRow, provider: EmailProvider | { name: "none"; reason: string },
  attachPdf: boolean): Promise<string> {
  if (!("send" in provider)) {
    if (environment() === "production") {
      await sql()`UPDATE email_outbox SET status = 'queued', attempts = attempts - 1, claimed_until = NULL,
                         not_before = now() + interval '15 minutes', last_error = ${provider.reason}, updated_at = now()
                   WHERE delivery_id = ${claimed.delivery_id}`;
      return "no_provider";
    }
    await suppress(claimed, `not sent: ${provider.reason} in this ${environment()} deployment`);
    return "suppressed";
  }
  const allowed = mayDeliver(provider, claimed.environment);
  if (!allowed.ok) {
    await suppress(claimed, `not sent: ${allowed.reason}`);
    return "suppressed";
  }
  if (claimed.recipient_id) {
    const [r] = await sql()<{ active: boolean; unsubscribed_at: string | null; suppressed_at: string | null;
      suppressed_reason: string | null }[]>`
      SELECT active, unsubscribed_at, suppressed_at, suppressed_reason FROM recipients
       WHERE recipient_id = ${claimed.recipient_id}`;
    const why = !r ? "the recipient was removed"
      : r.unsubscribed_at ? "the recipient unsubscribed"
      : !r.active ? "the recipient is inactive"
      : r.suppressed_at ? `the address is suppressed (${r.suppressed_reason ?? "earlier permanent failure"})` : null;
    if (why) {
      await suppress(claimed, why);
      return "suppressed";
    }
  }
  const row = await freeze(claimed, attachPdf);
  let attachment = null;
  if (row.attachment_key) {
    const bytes = await readPrivate(row.attachment_key, ATTACHMENT_LIMIT);
    if (!bytes) throw new Error("the PDF could not be read from storage; the message will be retried");
    attachment = { filename: row.attachment_name ?? "report.pdf", content: bytes };
  }
  const from = process.env.AZMONITOR_EMAIL_FROM ?? "Azerbaijan Monitor <onboarding@resend.dev>";
  const res = await provider.send({
    from, to: row.to_address, subject: row.subject!, text: row.body_text!, html: row.body_html ?? "",
    headers: headersOf(row), attachment,
    tags: [{ name: "purpose", value: row.purpose }, { name: "delivery", value: row.delivery_id }],
  }, row.idempotency_key);
  return recordResult(row, provider, res);
}

async function suppress(row: OutboxRow, reason: string) {
  await sql()`UPDATE email_outbox SET status = 'suppressed', status_reason = ${reason}, claimed_until = NULL,
                     attempts = greatest(attempts - 1, 0), updated_at = now()
               WHERE delivery_id = ${row.delivery_id}`;
}

// ------------------------------------------------------------------ provider events

const RANK: Record<string, number> = { queued: 0, sending: 1, accepted: 2, delivered: 3, failed: 4 };

export interface ProviderEvent {
  id: string;
  type: string;
  created_at: string;
  data: { email_id?: string; bounce?: { type?: string; message?: string }; failed?: { reason?: string };
    suppressed?: { message?: string; type?: string } };
}

/** Record a verified webhook event once, and apply it if its message is known. */
export async function recordEvent(provider: string, event: ProviderEvent): Promise<"duplicate" | "recorded"> {
  const messageId = event.data?.email_id ?? null;
  const inserted = await sql()<{ event_id: string }[]>`
    INSERT INTO email_events(event_id, provider, provider_message_id, event_type, occurred_at, payload)
    VALUES (${event.id}, ${provider}, ${messageId}, ${event.type}, ${event.created_at ?? null},
            ${sql().json(event as never)})
    ON CONFLICT (event_id) DO NOTHING RETURNING event_id`;
  if (inserted.length === 0) return "duplicate";
  if (messageId) await applyPendingEvents(provider, messageId);
  return "recorded";
}

/**
 * Apply every unapplied event for one message, oldest first. Called when an event arrives and again
 * when a send result is recorded, because a delivery webhook can arrive before the send call that
 * produced it has returned.
 */
export async function applyPendingEvents(provider: string, messageId: string) {
  await sql().begin(async (tx) => {
    const [row] = await tx<OutboxRow[]>`
      SELECT * FROM email_outbox WHERE provider = ${provider} AND provider_message_id = ${messageId} FOR UPDATE`;
    if (!row) return;
    const events = await tx<{ event_id: string; event_type: string; occurred_at: string | null; payload: ProviderEvent }[]>`
      SELECT event_id, event_type, occurred_at, payload FROM email_events
       WHERE provider = ${provider} AND provider_message_id = ${messageId} AND NOT applied
       ORDER BY occurred_at NULLS FIRST, received_at`;
    let status = row.status;
    let lastEvent = row.last_event;
    let lastAt = row.last_event_at ? new Date(row.last_event_at).getTime() : 0;
    let deliveredAt: string | null = null;
    let failedAt: string | null = null;
    let error: string | null = null;
    let suppressReason: string | null = null;
    for (const e of events) {
      const at = e.occurred_at ? new Date(e.occurred_at).getTime() : Date.now();
      const target = {
        "email.sent": "accepted", "email.delivered": "delivered", "email.bounced": "failed",
        "email.failed": "failed", "email.suppressed": "failed",
      }[e.event_type];
      if (target && (RANK[target] ?? 0) > (RANK[status] ?? 0)) {
        status = target;
        if (target === "delivered") deliveredAt = e.occurred_at;
        if (target === "failed") {
          failedAt = e.occurred_at;
          error = e.payload.data?.bounce?.message ?? e.payload.data?.failed?.reason
            ?? e.payload.data?.suppressed?.message ?? e.event_type;
        }
      }
      if (e.event_type === "email.bounced" && /permanent/i.test(e.payload.data?.bounce?.type ?? "")) {
        suppressReason = `permanent bounce: ${e.payload.data?.bounce?.message ?? ""}`.trim();
      }
      if (e.event_type === "email.suppressed") suppressReason = `provider suppression: ${e.payload.data?.suppressed?.message ?? ""}`.trim();
      if (e.event_type === "email.complained") suppressReason = "the recipient marked a message as spam";
      if (at >= lastAt) {
        lastAt = at;
        lastEvent = e.event_type;
      }
    }
    await tx`UPDATE email_outbox SET status = ${status}, last_event = ${lastEvent},
                    last_event_at = ${lastAt ? new Date(lastAt).toISOString() : null},
                    delivered_at = coalesce(delivered_at, ${deliveredAt}::timestamptz),
                    failed_at = coalesce(failed_at, ${failedAt}::timestamptz),
                    last_error = coalesce(${error}, last_error), updated_at = now()
              WHERE delivery_id = ${row.delivery_id}`;
    await tx`UPDATE email_events SET applied = true
              WHERE provider = ${provider} AND provider_message_id = ${messageId} AND NOT applied`;
    if (suppressReason && row.recipient_id) {
      await tx`UPDATE recipients SET suppressed_at = coalesce(suppressed_at, now()),
                      suppressed_reason = coalesce(suppressed_reason, ${suppressReason}), updated_at = now()
                WHERE recipient_id = ${row.recipient_id}`;
    }
  });
}

// ------------------------------------------------------------------ a person acting on the ledger

/** Retry a failed delivery, independently of the job and of every other delivery. */
export async function retryDelivery(deliveryId: string, actor: string): Promise<boolean> {
  const rows = await sql()<{ delivery_id: string }[]>`
    UPDATE email_outbox SET status = 'queued', not_before = now(), max_attempts = attempts + 3,
           last_error = ${`retry requested by ${actor}`}, updated_at = now()
     WHERE delivery_id = ${deliveryId} AND status = 'failed'
    RETURNING delivery_id`;
  return rows.length === 1;
}

/**
 * Settle an uncertain delivery. "sent" records that the person confirmed it arrived; "resend"
 * sends it again under a NEW key — deliberately, because the old key's window has passed and the
 * person has accepted the risk of a duplicate.
 */
export async function resolveUncertain(deliveryId: string, actor: string, decision: "sent" | "resend") {
  if (decision === "sent") {
    await sql()`UPDATE email_outbox SET status = 'accepted', last_error = ${`confirmed as sent by ${actor}`},
                       updated_at = now() WHERE delivery_id = ${deliveryId} AND status = 'uncertain'`;
    return;
  }
  const fresh = newId("idk");
  await sql()`UPDATE email_outbox SET status = 'queued', idempotency_key = ${fresh}, not_before = now(),
                     max_attempts = attempts + 3, last_attempt_ambiguous = false,
                     last_error = ${`resend requested by ${actor}`}, updated_at = now()
               WHERE delivery_id = ${deliveryId} AND status = 'uncertain'`;
}

export async function ledger(env: Env, limit = 100) {
  return sql()<(OutboxRow & { edition_label: string | null })[]>`
    SELECT o.*, p.report_type || ' ' || p.edition || ' v' || p.version AS edition_label
      FROM email_outbox o LEFT JOIN publication_records p ON p.edition_id = o.edition_id
     WHERE o.environment = ${env}
     ORDER BY o.created_at DESC LIMIT ${limit}`;
}

export async function deliveriesFor(opts: { jobId?: string; editionId?: string }) {
  return sql()<OutboxRow[]>`
    SELECT * FROM email_outbox
     WHERE (${opts.jobId ?? null}::text IS NOT NULL AND job_id = ${opts.jobId ?? null}::text)
        OR (${opts.editionId ?? null}::text IS NOT NULL AND edition_id = ${opts.editionId ?? null}::text)
     ORDER BY created_at`;
}
