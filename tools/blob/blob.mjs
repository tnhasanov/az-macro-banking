#!/usr/bin/env node
/**
 * Private Vercel Blob, for the Python engine.
 *
 * The engine is Python and Vercel publishes no Python SDK, so the alternative to this was
 * re-implementing the REST protocol by hand. That is a bad trade for one specific reason: the
 * dataset is about 260 MB, and a single PUT of 260 MB is not how the API expects to receive it.
 * `@vercel/blob` can split a large body into a multipart upload, retry the parts that fail and
 * raise a typed error for the ones that cannot be retried. Re-deriving that from observed
 * behaviour is exactly the guesswork worth avoiding, so this delegates to the SDK Vercel maintains.
 *
 * Note that multipart is opt-in, not automatic. `put()` streams a single request unless
 * `multipart: true` is passed, so a 260 MB upload that failed at 95% would restart from nothing.
 * MULTIPART_THRESHOLD below is what turns it on, and it is the whole reason the SDK is worth the
 * detour — without it this would be a slower way to make the same single request.
 *
 * Every object is written with `access: 'private'`. A private blob has no publicly readable URL:
 * reads carry the token in an Authorization header, and the token stays in this process — it is
 * never placed in a URL, never printed, and never returned to the caller.
 *
 * Payloads move as files rather than through stdout, because a pipe is the wrong shape for
 * hundreds of megabytes and stdout is reserved for the one JSON line each command returns.
 *
 * Exit codes are the interface, because the caller is a Python subprocess:
 *   0  fine
 *   3  the object is not there (a fact, not a failure — `get` and `head` both use it)
 *   4  refused: the object exists and this call may not overwrite it
 *   1  anything else
 */
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, rename, rm, stat } from "node:fs/promises";
import { dirname } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { put, get, head, list, del, BlobNotFoundError } from "@vercel/blob";

const ACCESS = "private";
const NOT_FOUND = 3;
const REFUSED = 4;

/**
 * Above this, upload in parts.
 *
 * The SDK's part size is 8 MB, so 8 MB is the smallest body that can become more than one part.
 * Below it multipart would add round trips for nothing; above it each part is retried on its own
 * and a failure late in a long upload costs one part rather than the whole transfer. The dataset's
 * static half (~254 MB) is the case this exists for; its mutable half (~6 MB) and every report file
 * stay single-request.
 */
const MULTIPART_THRESHOLD = 8 * 1024 * 1024;

function token() {
  const t = process.env.BLOB_READ_WRITE_TOKEN;
  if (!t) {
    fail("BLOB_READ_WRITE_TOKEN is not set; private Blob cannot be used", 1);
  }
  return t;
}

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

function fail(message, code = 1) {
  // stderr, so a failure can never be mistaken for the JSON result on stdout.
  process.stderr.write(`${message}\n`);
  process.exit(code);
}

function args(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const name = a.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) out[name] = true;
    else { out[name] = next; i += 1; }
  }
  return out;
}

/**
 * `head` is the existence check: one request, metadata rather than a body.
 *
 * Matched with `instanceof`, not by name. `BlobNotFoundError` does not set `.name` — an instance
 * reports `"Error"`, and only `constructor.name` says otherwise — so a check on `error.name` is
 * always false. That turned every "is this key already there?" into a thrown storage failure, which
 * would have broken the first upload of every edition: `publish_edition` asks exactly this question
 * before writing anything.
 */
async function exists(key) {
  try {
    return await head(key, { token: token() });
  } catch (error) {
    if (error instanceof BlobNotFoundError) return null;
    throw error;
  }
}

async function cmdPut(o) {
  if (!o.key || !o.file) fail("put needs --key and --file");
  const size = (await stat(o.file)).size;

  // An edition version is immutable, so an existing key is refused rather than replaced. Checked
  // here as well as by `allowOverwrite` because the check should be explicit about *why*.
  if (!o.overwrite) {
    const already = await exists(o.key);
    if (already) {
      fail(`${o.key} already exists and this call may not overwrite it`, REFUSED);
    }
  }

  const multipart = size > MULTIPART_THRESHOLD;
  const result = await put(o.key, createReadStream(o.file), {
    access: ACCESS,
    token: token(),
    contentType: o.contentType || "application/octet-stream",
    addRandomSuffix: false,
    allowOverwrite: Boolean(o.overwrite),
    multipart,
    // The dataset is replaced wholesale and reports are immutable, so nothing benefits from a
    // long CDN life; a private blob is fetched through the dashboard anyway.
    cacheControlMaxAge: 0,
  });
  emit({ key: result.pathname, size, multipart, contentType: result.contentType ?? null });
}

async function cmdGet(o) {
  if (!o.key || !o.out) fail("get needs --key and --out");
  const found = await get(o.key, { access: ACCESS, token: token(), useCache: false });
  if (!found || !found.stream) process.exit(NOT_FOUND);

  // Written to a temporary name and moved into place, so a transfer that dies part way through
  // cannot leave a half file where a whole one is expected. The caller verifies the digest; this
  // makes sure it is verifying a complete artefact or nothing at all.
  await mkdir(dirname(o.out), { recursive: true });
  const partial = `${o.out}.partial`;
  try {
    await pipeline(Readable.fromWeb(found.stream), createWriteStream(partial));
  } catch (error) {
    await rm(partial, { force: true });
    throw error;
  }
  const size = (await stat(partial)).size;
  const expected = found.blob?.size ?? null;
  if (expected !== null && expected !== size) {
    await rm(partial, { force: true });
    fail(`${o.key} was truncated: expected ${expected} bytes, received ${size}`, 1);
  }
  await rename(partial, o.out);
  emit({ key: o.key, size, contentType: found.blob?.contentType ?? null });
}

async function cmdHead(o) {
  if (!o.key) fail("head needs --key");
  const found = await exists(o.key);
  if (!found) process.exit(NOT_FOUND);
  emit({ key: found.pathname, size: found.size, contentType: found.contentType,
         uploadedAt: found.uploadedAt });
}

async function cmdList(o) {
  const out = [];
  let cursor;
  do {
    const page = await list({
      token: token(), prefix: o.prefix || undefined, limit: 1000, cursor,
    });
    for (const b of page.blobs) {
      out.push({ key: b.pathname, size: b.size, uploaded_at: b.uploadedAt });
    }
    cursor = page.hasMore ? page.cursor : undefined;
  } while (cursor);
  emit({ blobs: out });
}

async function cmdDel(o) {
  if (!o.key) fail("del needs --key");
  await del(o.key, { token: token() });
  emit({ deleted: o.key });
}

const COMMANDS = { put: cmdPut, get: cmdGet, head: cmdHead, list: cmdList, del: cmdDel };

const [command, ...rest] = process.argv.slice(2);
const run = COMMANDS[command];
if (!run) fail(`unknown command ${command ?? "(none)"}; expected one of ${Object.keys(COMMANDS).join(", ")}`);

try {
  await run(args(rest));
} catch (error) {
  // The message can carry a pathname but never the token, which the SDK keeps in a header.
  fail(`${error?.name ?? "Error"}: ${error?.message ?? String(error)}`, 1);
}
