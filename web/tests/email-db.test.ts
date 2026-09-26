/**
 * The delivery ledger against a real Postgres: sending, retrying, uncertainty, recipients' choices
 * and provider webhooks. The provider is a stand-in that records what it was given and answers as
 * told; the capture provider writes real files. Local integration — no email leaves this machine.
 */
import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { freshDatabase, publish, skip } from "./helpers/db.ts";
import type { EmailProvider, OutgoingEmail, SendResult } from "../lib/email/provider.ts";

let drop: () => Promise<void> = async () => undefined;
let O: typeof import("../lib/email/outbox.ts");
let A: typeof import("../lib/appstate.ts");
let sql: ReturnType<typeof import("../lib/db.ts")["sql"]>;
const store = mkdtempSync(path.join(tmpdir(), "azm-store-"));
const mail = mkdtempSync(path.join(tmpdir(), "azm-mail-"));

test.afterEach(() => { process.env.AZMONITOR_ENVIRONMENT = "development"; });

before(async () => {
  if (skip) return;
  ({ drop } = await freshDatabase("webmail"));
  process.env.AZMONITOR_ENVIRONMENT = "development";
  process.env.AZMONITOR_OBJECT_STORE_DIR = store;
  process.env.AZMONITOR_EMAIL_PROVIDER = "capture";
  process.env.AZMONITOR_EMAIL_CAPTURE_DIR = mail;
  process.env.AZMONITOR_APP_URL = "https://monitor.example.az";
  process.env.AZMONITOR_SESSION_SECRET = "x".repeat(40);
  O = await import("../lib/email/outbox.ts");
  A = await import("../lib/appstate.ts");
  sql = (await import("../lib/db.ts")).sql();
  await A.ensureSchema();
  const pdfKey = "reports/monthly/2026-07/v1/job_x-a1/deck.pdf";
  mkdirSync(path.dirname(path.join(store, pdfKey)), { recursive: true });
  writeFileSync(path.join(store, pdfKey), Buffer.alloc(1000, 1));
  await publish(sql, {});
});
after(async () => { if (!skip) await drop(); });

/**
 * A stand-in for the real provider. Only production sends through a real provider, so the tests
 * that use it run as production with production rows; the preview test checks the opposite.
 */
function scripted(answers: SendResult[], name = "resend") {
  const sent: { message: OutgoingEmail; key: string }[] = [];
  const provider: EmailProvider = {
    name,
    async send(message, key) {
      sent.push({ message, key });
      return answers.shift() ?? { kind: "accepted", providerMessageId: `msg_${key}` };
    },
  };
  return { provider, sent };
}

async function row(id: string) {
  return (await sql<import("../lib/email/outbox.ts").OutboxRow[]>`SELECT * FROM email_outbox WHERE delivery_id = ${id}`)[0];
}

test("a copy to the requester carries the grounded content and only application links", { skip }, async () => {
  const id = await O.queueCopy("development", "monthly:2026-07:v1", "owner@example.az");
  const res = await O.drainOutbox("development", { deliveryId: id });
  assert.equal(res.outcomes[0].outcome, "accepted");
  const [file] = readdirSync(mail).filter((f) => f.includes(id));
  const captured = JSON.parse(readFileSync(path.join(mail, file), "utf8"));
  assert.equal(captured.to, "owner@example.az");
  assert.match(captured.text, /Loans to households grew 18\.2% y\/y\./);
  assert.match(captured.text, /Banking data: 2026-07-31/);
  assert.match(captured.text, /Consumer prices: 2026-08-31/);
  assert.match(captured.text, /Information cutoff: end of 2026-09-26 \(Asia\/Baku\)/);
  assert.match(captured.text, /published 2026-\d\d-\d\d \d\d:\d\d UTC/);
  assert.match(captured.text, /https:\/\/monitor\.example\.az\/reports\/monthly\/2026-07\/1/);
  assert.match(captured.text, /https:\/\/monitor\.example\.az\/api\/files\/reports\/monthly/);
  assert.match(captured.text, /Inputs cover different periods/);
  assert.doesNotMatch(captured.text + captured.html, /vercel-storage|blob\.vercel|token=|BLOB_/i);
  assert.deepEqual(captured.attachment, { filename: "deck.pdf", bytes: 1000 });
  const r = await row(id);
  assert.equal(r.status, "accepted");
  assert.equal(r.idempotency_key, id);
});

test("an uncertain send is retried with the same key and identical content", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  const id = await O.queueCopy("production", "monthly:2026-07:v1", "owner@example.az");
  const s = scripted([{ kind: "uncertain", message: "timeout" }]);
  await O.drainOutbox("production", { deliveryId: id, provider: s.provider });
  let r = await row(id);
  assert.equal(r.status, "queued");
  assert.equal(r.last_attempt_ambiguous, true);
  const firstHash = r.content_hash;
  await sql`UPDATE email_outbox SET not_before = now() WHERE delivery_id = ${id}`;
  await O.drainOutbox("production", { deliveryId: id, provider: s.provider });
  r = await row(id);
  assert.equal(r.status, "accepted");
  assert.equal(s.sent.length, 2);
  assert.equal(s.sent[0].key, s.sent[1].key);
  assert.equal(s.sent[0].message.text, s.sent[1].message.text);
  assert.equal(r.content_hash, firstHash);
});

test("an uncertain send outside the idempotency window stops and waits for a person", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  const id = await O.queueCopy("production", "monthly:2026-07:v1", "owner@example.az");
  await sql`UPDATE email_outbox SET first_attempt_at = now() - interval '30 hours' WHERE delivery_id = ${id}`;
  const s = scripted([{ kind: "uncertain", message: "connection reset" }]);
  await O.drainOutbox("production", { deliveryId: id, provider: s.provider });
  const r = await row(id);
  assert.equal(r.status, "uncertain");
  await O.resolveUncertain(id, "owner", "resend");
  const after = await row(id);
  assert.equal(after.status, "queued");
  assert.notEqual(after.idempotency_key, id);         // deliberately a new key, by a person's decision
});

test("a permanent rejection fails; a temporary one is retried later", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  const bad = await O.queueCopy("production", "monthly:2026-07:v1", "owner@example.az");
  await O.drainOutbox("production", { deliveryId: bad,
    provider: scripted([{ kind: "rejected", permanent: true, code: "validation_error", message: "bad address" }]).provider });
  assert.equal((await row(bad)).status, "failed");
  assert.ok(await O.retryDelivery(bad, "owner"));
  assert.equal((await row(bad)).status, "queued");

  const busy = await O.queueCopy("production", "monthly:2026-07:v1", "owner@example.az");
  await O.drainOutbox("production", { deliveryId: busy,
    provider: scripted([{ kind: "rejected", permanent: false, code: "rate_limit_exceeded", message: "slow down", retryAfterSeconds: 60 }]).provider });
  const r = await row(busy);
  assert.equal(r.status, "queued");
  assert.ok(new Date(r.not_before).getTime() > Date.now() + 30_000);
});

test("an unsubscribed recipient is not sent to, even if the message was queued before", { skip }, async () => {
  const rid = await A.upsertRecipient("owner", { email: "reader@example.az", role: "subscriber", active: true,
    subscriptions: [{ report_type: "monthly", sector: "*" }] });
  const id = "dlv_" + "0".repeat(26);
  await sql`INSERT INTO email_outbox(delivery_id, environment, edition_id, recipient_id, to_address, purpose, status, idempotency_key)
            VALUES (${id}, 'development', 'monthly:2026-07:v1', ${rid}, 'reader@example.az', 'new_edition', 'queued', ${id})`;
  await A.unsubscribe(rid, "test");
  assert.equal((await row(id)).status, "cancelled");
});

test("pausing holds announcements but not a copy someone asked for", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  const rid = await A.upsertRecipient("owner", { email: "paused@example.az", role: "subscriber", active: true,
    subscriptions: [{ report_type: "monthly", sector: "*" }] });
  const announce = "dlv_" + "1".repeat(26);
  await sql`INSERT INTO email_outbox(delivery_id, environment, edition_id, recipient_id, to_address, purpose, status, idempotency_key)
            VALUES (${announce}, 'production', 'monthly:2026-07:v1', ${rid}, 'paused@example.az', 'new_edition', 'queued', ${announce})`;
  const copy = await O.queueCopy("production", "monthly:2026-07:v1", "owner@example.az");
  await A.saveSettings("production", "owner", { paused: true });
  const s = scripted([]);
  await O.drainOutbox("production", { provider: s.provider });
  assert.equal((await row(announce)).status, "queued");
  assert.equal((await row(copy)).status, "accepted", (await row(copy)).last_error ?? "");
  await A.saveSettings("production", "owner", { paused: false });
  await O.drainOutbox("production", { provider: s.provider });
  assert.equal((await row(announce)).status, "accepted");
  const unsub = s.sent.find((x) => x.message.to === "paused@example.az")!;
  assert.match(unsub.message.headers!["List-Unsubscribe"], /\/api\/unsubscribe\?t=/);
  assert.match(unsub.message.text, /Stop these emails: https:\/\/monitor\.example\.az\/unsubscribe\?t=/);
});

test("a preview deployment records messages and sends none, whatever keys it has", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "preview";
  try {
    const id = await O.queueCopy("preview", "monthly:2026-07:v1", "owner@example.az");
    const s = scripted([]);
    await O.drainOutbox("preview", { deliveryId: id, provider: s.provider });
    assert.equal(s.sent.length, 0);
    const r = await row(id);
    assert.equal(r.status, "suppressed");
    assert.match(r.status_reason ?? "", /preview/);
  } finally {
    process.env.AZMONITOR_ENVIRONMENT = "development";
  }
});

test("a PDF over the limit is linked, not attached, and the email says so", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  await publish(sql, { edition: "2026-06", pdfBytes: 50 * 1024 * 1024 });
  const id = await O.queueCopy("production", "monthly:2026-06:v1", "owner@example.az");
  const s = scripted([]);
  await O.drainOutbox("production", { deliveryId: id, provider: s.provider });
  assert.equal(s.sent[0].message.attachment, null);
  assert.match(s.sent[0].message.text, /over the attachment limit, so it is linked/);
});

test("webhook events: out of order, duplicated, and a permanent bounce", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "production";
  const rid = await A.upsertRecipient("owner", { email: "bouncy@example.az", role: "subscriber", active: true,
    subscriptions: [{ report_type: "monthly", sector: "*" }] });
  const id = "dlv_" + "2".repeat(26);
  await sql`INSERT INTO email_outbox(delivery_id, environment, edition_id, recipient_id, to_address, purpose, status, idempotency_key)
            VALUES (${id}, 'production', 'monthly:2026-07:v1', ${rid}, 'bouncy@example.az', 'new_edition', 'queued', ${id})`;
  // the delivery event arrives before the send call has returned its message id
  assert.equal(await O.recordEvent("resend", { id: "evt_1", type: "email.delivered", created_at: "2026-09-26T10:00:05Z",
    data: { email_id: "msg_early" } }), "recorded");
  const s = scripted([{ kind: "accepted", providerMessageId: "msg_early" }]);
  await O.drainOutbox("production", { deliveryId: id, provider: s.provider });
  let r = await row(id);
  assert.equal(r.status, "delivered", `the early event is applied once the send is recorded (${r.last_error})`);
  assert.equal(await O.recordEvent("resend", { id: "evt_1", type: "email.delivered", created_at: "2026-09-26T10:00:05Z",
    data: { email_id: "msg_early" } }), "duplicate");

  await O.recordEvent("resend", { id: "evt_2", type: "email.bounced", created_at: "2026-09-26T10:00:09Z",
    data: { email_id: "msg_early", bounce: { type: "Permanent", message: "mailbox does not exist" } } });
  r = await row(id);
  assert.equal(r.status, "failed");
  // an older "sent" arriving late changes nothing
  await O.recordEvent("resend", { id: "evt_0", type: "email.sent", created_at: "2026-09-26T10:00:01Z",
    data: { email_id: "msg_early" } });
  assert.equal((await row(id)).status, "failed");
  const [rec] = await sql<{ suppressed_at: Date | null; suppressed_reason: string }[]>`
    SELECT suppressed_at, suppressed_reason FROM recipients WHERE recipient_id = ${rid}`;
  assert.ok(rec.suppressed_at);
  assert.match(rec.suppressed_reason, /permanent bounce/);

  // and the suppressed address is not sent to again
  const next = "dlv_" + "3".repeat(26);
  await sql`INSERT INTO email_outbox(delivery_id, environment, edition_id, recipient_id, to_address, purpose, status, idempotency_key)
            VALUES (${next}, 'production', 'monthly:2026-06:v1', ${rid}, 'bouncy@example.az', 'new_edition', 'queued', ${next})`;
  await O.drainOutbox("production", { deliveryId: next, provider: scripted([]).provider });
  assert.equal((await row(next)).status, "suppressed");
});
