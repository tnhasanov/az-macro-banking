/**
 * The file `vercel env pull` writes, and what may be taken from it.
 *
 * The case that made this necessary: `BLOB_READ_WRITE_TOKEN` comes back **present and empty**,
 * because Vercel stores it as a sensitive variable and a sensitive variable is write-only by
 * design — no reveal in the dashboard, no API that returns the plaintext. An empty credential
 * handed to the SDK fails as an authentication error somewhere far from its cause, so it has to be
 * refused here, by name, with the reason.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";

import { parseEnvFile, extract } from "./oidc-from-env-file.mjs";

const SCRIPT = new URL("./oidc-from-env-file.mjs", import.meta.url).pathname;

/** What the CLI actually writes: every value quoted, one per line. */
const PULLED = [
  'AZMONITOR_DATABASE_URL="postgres://user:pw@ep-x-pooler.eu-central-1.aws.neon.tech/db"',
  'BLOB_READ_WRITE_TOKEN=""',
  'BLOB_STORE_ID="store_AbCdEf123456"',
  'BLOB_WEBHOOK_PUBLIC_KEY="MCowBQYDK2VwAyEA"',
  'VERCEL_OIDC_TOKEN="eyJhbGciOiJSUzI1NiJ9.payload.signature"',
  "",
].join("\n");

function run(text, env = {}) {
  const dir = mkdtempSync(join(tmpdir(), "pulled-"));
  const file = join(dir, ".env.pulled");
  writeFileSync(file, text);
  try {
    const stdout = execFileSync(process.execPath, [SCRIPT, file],
      { env: { ...process.env, GITHUB_ENV: "", ...env }, encoding: "utf8" });
    return { ok: true, stdout, dir };
  } catch (error) {
    return { ok: false, stderr: String(error.stderr), status: error.status, dir };
  }
}

test("a quoted value is read without its quotes", () => {
  const parsed = parseEnvFile(PULLED);
  assert.equal(parsed.BLOB_STORE_ID, "store_AbCdEf123456");
  assert.equal(parsed.VERCEL_OIDC_TOKEN, "eyJhbGciOiJSUzI1NiJ9.payload.signature");
});

test("a quoted empty value is empty, not two quote characters", () => {
  // `BLOB_READ_WRITE_TOKEN=""` read naively is the string `""` — truthy, and sent as a credential.
  assert.equal(parseEnvFile(PULLED).BLOB_READ_WRITE_TOKEN, "");
  assert.equal(extract(PULLED).sawReadWriteKey, true);
  assert.deepEqual(extract(PULLED).missing, []);
});

test("the read-write token is never taken, empty or not", () => {
  const withValue = PULLED.replace('BLOB_READ_WRITE_TOKEN=""',
    'BLOB_READ_WRITE_TOKEN="vercel_blob_rw_AbCdEf123456_secret"');
  assert.ok(!("BLOB_READ_WRITE_TOKEN" in extract(withValue).found),
    "this path mints its own short-lived credential; it does not carry a static one");
});

test("the OIDC token and the store id are both taken", () => {
  const { found } = extract(PULLED);
  assert.equal(found.BLOB_STORE_ID, "store_AbCdEf123456");
  assert.equal(found.VERCEL_OIDC_TOKEN, "eyJhbGciOiJSUzI1NiJ9.payload.signature");
});

test("a missing OIDC token is named, not guessed around", () => {
  const without = PULLED.split("\n").filter((l) => !l.startsWith("VERCEL_OIDC_TOKEN")).join("\n");
  const result = run(without);
  assert.equal(result.ok, false);
  assert.match(result.stderr, /VERCEL_OIDC_TOKEN/);
  assert.match(result.stderr, /empty BLOB_READ_WRITE_TOKEN is expected/,
    "the message must say the empty token is not the problem, because that is what it looks like");
});

test("a store that is not connected is named too", () => {
  const without = PULLED.split("\n").filter((l) => !l.startsWith("BLOB_STORE_ID")).join("\n");
  const result = run(without);
  assert.equal(result.ok, false);
  assert.match(result.stderr, /BLOB_STORE_ID/);
  assert.match(result.stderr, /connected to this project/);
});

test("the token is masked before anything else is printed", () => {
  const { stdout } = run(PULLED);
  const lines = stdout.trim().split("\n");
  assert.equal(lines[0], "::add-mask::eyJhbGciOiJSUzI1NiJ9.payload.signature",
    "masking must be the first thing on stdout or a later failure can echo the token");
});

test("the summary names the store but never the token", () => {
  const { stdout } = run(PULLED);
  const summary = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(summary.credential, "oidc");
  assert.equal(summary.store_id, "store_AbCdEf123456");
  assert.equal(summary.oidc_token_length, "eyJhbGciOiJSUzI1NiJ9.payload.signature".length);
  assert.ok(!JSON.stringify(summary).includes("eyJhbGciOiJSUzI1NiJ9"),
    "a length says the token arrived; the value says it to anyone reading the log");
});

test("only the two variables are exported, never the rest of the pulled file", () => {
  const dir = mkdtempSync(join(tmpdir(), "ghenv-"));
  const githubEnv = join(dir, "github.env");
  writeFileSync(githubEnv, "");
  const result = run(PULLED, { GITHUB_ENV: githubEnv });
  assert.equal(result.ok, true);
  const written = readFileSync(githubEnv, "utf8");
  assert.match(written, /^VERCEL_OIDC_TOKEN=eyJhbGciOiJSUzI1NiJ9\.payload\.signature$/m);
  assert.match(written, /^BLOB_STORE_ID=store_AbCdEf123456$/m);
  assert.ok(!written.includes("AZMONITOR_DATABASE_URL"),
    "the pulled file holds every secret in the environment; only these two travel on");
  assert.ok(!written.includes("BLOB_READ_WRITE_TOKEN"));
});
