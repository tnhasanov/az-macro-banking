/**
 * Tests for the gate.
 *
 * These are the cases where being wrong means someone gets in, so they are stated as attacks rather
 * than as features: a wrong passphrase, a tampered digest, a missing secret, a cron call with no
 * bearer, a bearer that is a prefix of the real one.
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  authorizeScheduler, constantTimeStringEqual, hashPassphrase, issueSession, readSession,
  timingSafeEqual, verifyPassphrase,
} from "../lib/auth.ts";

const SECRET = "a-test-secret-that-is-comfortably-longer-than-thirty-two-characters";

/**
 * Run `fn` with these variables set, then put the environment back.
 *
 * Awaits the result before restoring. The code under test reads `process.env` at call time — which
 * is what a serverless runtime requires — so restoring while an async body is still in flight would
 * pull the secret out from under it and fail a test that should pass.
 */
async function withEnv<T>(
  env: Record<string, string | undefined>,
  fn: () => T | Promise<T>,
): Promise<T> {
  const saved: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(env)) {
    saved[k] = process.env[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  try {
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

// A low iteration count keeps the suite fast; the format carries the cost, so this proves the
// stored digest is self-describing rather than that 600,000 rounds are slow.
const FAST = 1000;

test("the right passphrase verifies and the wrong one does not", async () => {
  const stored = await hashPassphrase("correct horse battery staple", FAST);
  assert.equal(await verifyPassphrase("correct horse battery staple", stored), true);
  assert.equal(await verifyPassphrase("correct horse battery stapl", stored), false);
  assert.equal(await verifyPassphrase("", stored), false);
  assert.equal(await verifyPassphrase("Correct Horse Battery Staple", stored), false);
});

test("the passphrase itself is never in the stored digest", async () => {
  const stored = await hashPassphrase("hunter2-and-then-some", FAST);
  assert.ok(!stored.includes("hunter2"));
  assert.match(stored, /^pbkdf2\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$/);
});

test("two digests of the same passphrase differ, because the salt is fresh", async () => {
  const a = await hashPassphrase("same passphrase", FAST);
  const b = await hashPassphrase("same passphrase", FAST);
  assert.notEqual(a, b);
  assert.equal(await verifyPassphrase("same passphrase", a), true);
  assert.equal(await verifyPassphrase("same passphrase", b), true);
});

test("a malformed or tampered digest is refused, never accepted by accident", async () => {
  for (const bad of [
    "", "not-a-digest", "pbkdf2$notanumber$c2FsdA==$aGFzaA==", "pbkdf2$0$c2FsdA==$aGFzaA==",
    "scrypt$600000$c2FsdA==$aGFzaA==", "pbkdf2$1000$c2FsdA==",
  ]) {
    assert.equal(await verifyPassphrase("anything", bad), false, bad);
  }
});

test("the stored cost is honoured, so it can be raised without invalidating a digest", async () => {
  const stored = await hashPassphrase("cost-sensitive", 2000);
  assert.ok(stored.startsWith("pbkdf2$2000$"));
  assert.equal(await verifyPassphrase("cost-sensitive", stored), true);
});

test("a session round-trips, and a tampered or foreign token does not", async () => {
  await withEnv({ AZMONITOR_SESSION_SECRET: SECRET }, async () => {
    const { token, maxAge } = await issueSession("owner@example.test");
    assert.ok(maxAge > 0);
    const session = await readSession(token);
    assert.equal(session?.sub, "owner@example.test");

    // A flipped character in the signature must not verify.
    const tampered = token.slice(0, -2) + (token.endsWith("aa") ? "bb" : "aa");
    assert.equal(await readSession(tampered), null);
    assert.equal(await readSession("not.a.token"), null);
    assert.equal(await readSession(undefined), null);
    assert.equal(await readSession(""), null);
  });
});

test("a token signed with another secret is rejected", async () => {
  const token = await withEnv({ AZMONITOR_SESSION_SECRET: SECRET }, () =>
    issueSession("owner").then((r) => r.token));
  const other = `${SECRET}-but-different-entirely-and-long-enough`;
  const result = await withEnv({ AZMONITOR_SESSION_SECRET: other }, () => readSession(token));
  assert.equal(result, null);
});

test("a short or missing session secret refuses to sign rather than signing weakly", async () => {
  await assert.rejects(
    () => withEnv({ AZMONITOR_SESSION_SECRET: "too-short" }, () => issueSession("owner")),
    /shorter than 32/,
  );
  await assert.rejects(
    () => withEnv({ AZMONITOR_SESSION_SECRET: undefined }, () => issueSession("owner")),
    /missing/,
  );
});

test("comparisons do not leak where the difference is", () => {
  const enc = new TextEncoder();
  assert.equal(timingSafeEqual(enc.encode("abc"), enc.encode("abc")), true);
  assert.equal(timingSafeEqual(enc.encode("abc"), enc.encode("abd")), false);
  // Different lengths are not equal, and a prefix is not a match.
  assert.equal(timingSafeEqual(enc.encode("abc"), enc.encode("ab")), false);
  assert.equal(constantTimeStringEqual("secret", "secret"), true);
  assert.equal(constantTimeStringEqual("secret", "secre"), false);
  assert.equal(constantTimeStringEqual("secret", "secretx"), false);
});

function cronRequest(authorization?: string): Request {
  return new Request("https://example.test/api/cron/monitor", {
    headers: authorization ? { authorization } : {},
  });
}

test("a scheduled trigger needs the bearer secret and nothing else will do", async () => {
  const secret = "a-cron-secret-long-enough-to-be-real";
  await withEnv({ CRON_SECRET: secret }, () => {
    assert.equal(authorizeScheduler(cronRequest(`Bearer ${secret}`)).ok, true);
    assert.equal(authorizeScheduler(cronRequest()).ok, false);
    assert.equal(authorizeScheduler(cronRequest("Bearer wrong")).ok, false);
    // A prefix of the secret is not the secret.
    assert.equal(authorizeScheduler(cronRequest(`Bearer ${secret.slice(0, -1)}`)).ok, false);
    // The scheme matters: the platform's own header is not an authorisation.
    assert.equal(authorizeScheduler(cronRequest(secret)).ok, false);
    assert.equal(authorizeScheduler(cronRequest(`Basic ${secret}`)).ok, false);
  });
});

test("with no cron secret configured, nothing is authorised to trigger a run", async () => {
  await withEnv({ CRON_SECRET: undefined }, () => {
    const result = authorizeScheduler(cronRequest("Bearer anything"));
    assert.equal(result.ok, false);
    assert.match(result.reason ?? "", /not configured/);
  });
  // A secret too short to be meaningful is treated as none at all.
  await withEnv({ CRON_SECRET: "short" }, () => {
    assert.equal(authorizeScheduler(cronRequest("Bearer short")).ok, false);
  });
});
