/**
 * The API routes as a browser (or an attacker) meets them: signed in or not, same origin or not,
 * administrator or not. Handlers are called directly with real Request objects against a real
 * Postgres. Dispatch is off in this environment, so no runner is started.
 */
import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { Webhook } from "standardwebhooks";
import { freshDatabase, skip } from "./helpers/db.ts";

let drop: () => Promise<void> = async () => undefined;
let cookie = "";
let sql: ReturnType<typeof import("../lib/db.ts")["sql"]>;
const HOST = "monitor.example.az";

before(async () => {
  if (skip) return;
  ({ drop } = await freshDatabase("webroutes"));
  process.env.AZMONITOR_ENVIRONMENT = "development";
  process.env.AZMONITOR_DISPATCH_MODE = "off";
  process.env.AZMONITOR_SESSION_SECRET = "s".repeat(48);
  process.env.AZMONITOR_OWNER_EMAIL = "owner@example.az";
  process.env.RESEND_WEBHOOK_SECRET = "whsec_" + Buffer.from("0123456789abcdef0123456789abcdef").toString("base64");
  process.env.CRON_SECRET = "c".repeat(32);
  const { issueSession, SESSION_COOKIE } = await import("../lib/auth.ts");
  const { token } = await issueSession("owner@example.az");
  cookie = `${SESSION_COOKIE}=${token}`;
  sql = (await import("../lib/db.ts")).sql();
});
after(async () => { if (!skip) await drop(); });

function req(path: string, init: { method?: string; body?: unknown; cookie?: string | null; origin?: string | null;
  type?: string; headers?: Record<string, string> } = {}) {
  const headers: Record<string, string> = { host: HOST, ...(init.headers ?? {}) };
  if (init.cookie !== null) headers.cookie = init.cookie ?? cookie;
  if (init.origin !== null) headers.origin = init.origin ?? `https://${HOST}`;
  if (init.body !== undefined) headers["content-type"] = init.type ?? "application/json";
  return new Request(`https://${HOST}${path}`, { method: init.method ?? (init.body !== undefined ? "POST" : "GET"),
    headers, body: init.body === undefined ? undefined : typeof init.body === "string" ? init.body : JSON.stringify(init.body) });
}

const jobs = () => import("../app/api/jobs/route.ts");
const monthly = { report_type: "monthly", params: { period: "latest", refresh: "latest_data" } };

test("without a session every route refuses", { skip }, async () => {
  const { POST, GET } = await jobs();
  assert.equal((await POST(req("/api/jobs", { body: monthly, cookie: null }), {})).status, 401);
  assert.equal((await GET(req("/api/jobs", { cookie: null }), {})).status, 401);
  const settings = await import("../app/api/settings/route.ts");
  assert.equal((await settings.POST(req("/api/settings", { body: { auto_email_enabled: true }, cookie: null }), {})).status, 401);
  const send = await import("../app/api/editions/send/route.ts");
  assert.equal((await send.POST(req("/api/editions/send", { body: { edition_id: "monthly:2026-07:v1" }, cookie: null }), {})).status, 401);
});

test("a forged session is refused", { skip }, async () => {
  const { POST } = await jobs();
  const res = await POST(req("/api/jobs", { body: monthly, cookie: "azmonitor_session=eyJhbGciOiJub25lIn0.e30." }), {});
  assert.equal(res.status, 401);
});

test("a cross-site request is refused even with a valid session", { skip }, async () => {
  const { POST } = await jobs();
  assert.equal((await POST(req("/api/jobs", { body: monthly, origin: "https://evil.example" }), {})).status, 403);
  assert.equal((await POST(req("/api/jobs", { body: monthly, origin: null, headers: { "sec-fetch-site": "cross-site" } }), {})).status, 403);
  assert.equal((await POST(req("/api/jobs", { body: "report_type=monthly", type: "application/x-www-form-urlencoded" }), {})).status, 415);
});

test("a request with unexpected parameters is refused with the reason", { skip }, async () => {
  const { POST } = await jobs();
  const res = await POST(req("/api/jobs", { body: { report_type: "monthly", params: { period: "latest", branch: "main" } } }), {});
  assert.equal(res.status, 400);
  assert.equal((await res.json()).code, "unknown_parameter");
  const future = await POST(req("/api/jobs", { body: { report_type: "monthly", params: { period: "2099-01" } } }), {});
  assert.equal(future.status, 422);
});

test("a signed-in request creates one job, and a second identical one attaches to it", { skip }, async () => {
  const { POST } = await jobs();
  const first = await POST(req("/api/jobs", { body: { ...monthly, email_me: true } }), {});
  assert.equal(first.status, 201);
  const a = await first.json();
  assert.match(a.job_id, /^job_[0-9a-hjkmnp-tv-z]{26}$/);
  assert.match(a.dispatch, /No runner was started/);
  const second = await POST(req("/api/jobs", { body: monthly }), {});
  assert.equal(second.status, 200);
  assert.equal((await second.json()).job_id, a.job_id);
  const [job] = await sql<{ requester_email: string; notify_requester: boolean }[]>`
    SELECT requester_email, notify_requester FROM report_jobs WHERE job_id = ${a.job_id}`;
  assert.deepEqual(job, { requester_email: "owner@example.az", notify_requester: true });

  const detail = await import("../app/api/jobs/[id]/route.ts");
  const res = await detail.GET(req(`/api/jobs/${a.job_id}`), { params: Promise.resolve({ id: a.job_id }) });
  const view = await res.json();
  assert.equal(view.job.status, "queued");
  assert.ok(!("error_detail" in view.job), "the worker's error detail never reaches the browser");
  assert.deepEqual(view.stages.map((s: { id: string }) => s.id),
    ["queued", "collecting", "calculating", "writing_narrative", "rendering", "validating", "uploading", "complete"]);
  assert.equal(view.stages[1].state, "skipped");
});

test("forcing a regeneration needs an administrator and a reason", { skip }, async () => {
  const { POST } = await jobs();
  const noReason = await POST(req("/api/jobs", { body: { ...monthly, force_reason: "x" } }), {});
  assert.equal(noReason.status, 400);
  process.env.AZMONITOR_ADMIN_SUBJECTS = "someone-else@example.az";
  try {
    const notAdmin = await POST(req("/api/jobs", { body: { ...monthly, force_reason: "layout fix on the deposits slide" } }), {});
    assert.equal(notAdmin.status, 403);
    const settings = await import("../app/api/settings/route.ts");
    assert.equal((await settings.POST(req("/api/settings", { body: { auto_email_enabled: true } }), {})).status, 403);
  } finally {
    delete process.env.AZMONITOR_ADMIN_SUBJECTS;
  }
  const ok = await POST(req("/api/jobs", { body: { ...monthly, force_reason: "layout fix on the deposits slide" } }), {});
  assert.equal(ok.status, 201);
  const [row] = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM audit_log WHERE action = 'force_regenerate'`;
  assert.equal(row.n, 1);
});

test("the scheduler endpoint needs its bearer secret", { skip }, async () => {
  const tick = await import("../app/api/cron/tick/route.ts");
  assert.equal((await tick.GET(req("/api/cron/tick", { cookie: null }))).status, 401);
  assert.equal((await tick.GET(req("/api/cron/tick", { headers: { authorization: "Bearer wrong" } }))).status, 401);
  const ok = await tick.GET(req("/api/cron/tick", { cookie: null, headers: { authorization: `Bearer ${"c".repeat(32)}` } }));
  assert.equal(ok.status, 200);
  assert.match((await ok.json()).scheduling, /only in production/);
});

test("webhooks: an unsigned or tampered event is refused; a signed one is recorded once", { skip }, async () => {
  const hook = await import("../app/api/webhooks/resend/route.ts");
  const payload = JSON.stringify({ type: "email.delivered", created_at: "2026-09-26T10:00:00Z", data: { email_id: "msg_1" } });
  const signer = new Webhook(process.env.RESEND_WEBHOOK_SECRET!);
  const now = new Date();
  const signature = signer.sign("msg_evt_1", now, payload);
  const signed = (body: string, sig = signature) => new Request(`https://${HOST}/api/webhooks/resend`, { method: "POST",
    headers: { "svix-id": "msg_evt_1", "svix-timestamp": String(Math.floor(now.getTime() / 1000)), "svix-signature": sig,
      "content-type": "application/json" }, body });
  assert.equal((await hook.POST(new Request(`https://${HOST}/api/webhooks/resend`, { method: "POST", body: payload }))).status, 401);
  assert.equal((await hook.POST(signed(payload.replace("delivered", "bounced")))).status, 401);
  const first = await hook.POST(signed(payload));
  assert.equal(first.status, 200);
  assert.equal((await first.json()).outcome, "recorded");
  assert.equal((await (await hook.POST(signed(payload))).json()).outcome, "duplicate");
});

test("an unsubscribe link must carry a valid signature", { skip }, async () => {
  const route = await import("../app/api/unsubscribe/route.ts");
  const { sign } = await import("../lib/signing.ts");
  const { upsertRecipient } = await import("../lib/appstate.ts");
  const rid = await upsertRecipient("owner", { email: "r@example.az", role: "subscriber", active: true, subscriptions: [] });
  assert.equal((await route.POST(new Request(`https://${HOST}/api/unsubscribe?t=${rid}.forged`, { method: "POST" }))).status, 400);
  const ok = await route.POST(new Request(`https://${HOST}/api/unsubscribe?t=${encodeURIComponent(sign("unsubscribe", rid))}`, { method: "POST" }));
  assert.equal(ok.status, 200);
  const [r] = await sql<{ unsubscribed_at: Date | null }[]>`SELECT unsubscribed_at FROM recipients WHERE recipient_id = ${rid}`;
  assert.ok(r.unsubscribed_at);
});
