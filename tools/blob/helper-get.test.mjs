/**
 * The helper's own download command, run as a process, against the stand-in service
 * (fake-store.mjs) answering the way the real one does.
 *
 * On 2026-09-28 the first real seed uploaded the dataset and then failed to re-download it for the
 * backup: the service sends a large private blob Brotli-compressed with no Content-Length, the SDK
 * reports its size as 0, and the helper called a 5,851,807-byte archive truncated. The same path is
 * every worker's restore. These tests pin the behaviour against a service that answers that way.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { randomBytes } from "node:crypto";

const HELPER = fileURLToPath(new URL("./blob.mjs", import.meta.url));
const FAKE = pathToFileURL(fileURLToPath(new URL("./fake-store.mjs", import.meta.url))).href;

function helper(dir, ...argv) {
  const env = { ...process.env, BLOB_READ_WRITE_TOKEN: "vercel_blob_rw_rehearsal_helperget",
                AZMONITOR_FAKE_BLOB_DIR: join(dir, "store"), NODE_OPTIONS: `--import=${FAKE}`,
                VERCEL_BLOB_RETRIES: "0" };
  for (const name of ["VERCEL_OIDC_TOKEN", "BLOB_STORE_ID", "AZMONITOR_BLOB_AUTH"]) delete env[name];
  const run = spawnSync(process.execPath, [HELPER, ...argv], { env, encoding: "utf8" });
  return { status: run.status, stderr: run.stderr, out: run.stdout.trim() ? JSON.parse(run.stdout) : null };
}

function roundTrip(bytes) {
  const dir = mkdtempSync(join(tmpdir(), "helper-get-"));
  writeFileSync(join(dir, "in.bin"), bytes);
  const put = helper(dir, "put", "--key", "dataset/x.tar.gz", "--file", join(dir, "in.bin"));
  assert.equal(put.status, 0, put.stderr);
  const head = helper(dir, "head", "--key", "dataset/x.tar.gz");
  const got = helper(dir, "get", "--key", "dataset/x.tar.gz", "--out", join(dir, "out.bin"));
  return { dir, put, head, got };
}

test("a large object served compressed with no Content-Length downloads whole", () => {
  const bytes = randomBytes(300_000);
  const { dir, head, got } = roundTrip(bytes);
  assert.equal(got.status, 0, `the helper refused a complete download: ${got.stderr}`);
  assert.equal(got.out.size, bytes.length);
  assert.ok(readFileSync(join(dir, "out.bin")).equals(bytes), "the bytes written are the bytes stored");
  assert.equal(got.out.etag, head.out.etag,
    "the weak ETag of a compressed response names the same version as head's; it is reported in that form");
  assert.ok(!got.out.etag.startsWith("W/"));
  const asked = readFileSync(join(dir, "store", "downloads.log"), "utf8").trim().split("\n").pop();
  assert.equal(asked.split("\t")[1], "identity", "the download asks for the object as stored");
});

test("a small object served plain still downloads, with its own length checked", () => {
  const bytes = Buffer.from('{"version": 2}');
  const { dir, head, got } = roundTrip(bytes);
  assert.equal(got.status, 0, got.stderr);
  assert.ok(readFileSync(join(dir, "out.bin")).equals(bytes));
  assert.equal(got.out.etag, head.out.etag);
});

test("an upload that meets one failed attempt is retried whole, not abandoned", () => {
  // On 2026-09-30 two uploads in a row failed with "Response body object should not be disturbed
  // or locked": the SDK retried after a passing fault and the stream it had been given was spent.
  const dir = mkdtempSync(join(tmpdir(), "helper-put-retry-"));
  const bytes = randomBytes(120_000);
  writeFileSync(join(dir, "in.bin"), bytes);
  const env = { ...process.env, BLOB_READ_WRITE_TOKEN: "vercel_blob_rw_rehearsal_helperput",
                AZMONITOR_FAKE_BLOB_DIR: join(dir, "store"), NODE_OPTIONS: `--import=${FAKE}`,
                AZMONITOR_FAKE_BLOB_FAIL_FIRST_PUT: "1", VERCEL_BLOB_RETRIES: "2" };
  for (const name of ["VERCEL_OIDC_TOKEN", "BLOB_STORE_ID", "AZMONITOR_BLOB_AUTH"]) delete env[name];
  const put = spawnSync(process.execPath, [HELPER, "put", "--key", "reports/x/v1/a.pptx", "--file", join(dir, "in.bin")],
                        { env, encoding: "utf8" });
  assert.equal(put.status, 0, `the retry did not succeed: ${put.stderr}`);
  const got = helper(dir, "get", "--key", "reports/x/v1/a.pptx", "--out", join(dir, "out.bin"));
  assert.equal(got.status, 0, got.stderr);
  assert.ok(readFileSync(join(dir, "out.bin")).equals(bytes), "what arrived after the retry is the whole file");
});
