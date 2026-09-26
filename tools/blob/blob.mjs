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
 * reads carry the credential in an Authorization header, and it stays in this process — never
 * placed in a URL, never printed, and never returned to the caller. See `credentials()` for which
 * credential that is and why the answer depends on where this runs.
 *
 * Payloads move as files rather than through stdout, because a pipe is the wrong shape for
 * hundreds of megabytes and stdout is reserved for the one JSON line each command returns.
 *
 * Exit codes are the interface, because the caller is a Python subprocess:
 *   0  fine
 *   3  the object is not there (a fact, not a failure — `get` and `head` both use it)
 *   4  refused: the object exists and this call may not overwrite it
 *   1  anything else
 *
 * `check` is the one command that writes nothing anywhere: it proves the credential opens the
 * store, so a run that is going to fail on authentication fails before it spends an hour.
 */
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, rename, rm, stat } from "node:fs/promises";
import { dirname } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { pathToFileURL } from "node:url";
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

/**
 * What this process authenticates to the store with.
 *
 * A private store takes either credential, and which one is available is decided by where the code
 * runs, not by preference:
 *
 * * **OIDC** — `VERCEL_OIDC_TOKEN` plus `BLOB_STORE_ID`. Short-lived and rotated by Vercel, and the
 *   better credential by some distance. It is issued to Vercel's own runtimes and to the CLI on a
 *   linked project; `@vercel/oidc` reads it from the environment or refreshes it from the CLI's
 *   stored login. Neither exists on a GitHub Actions runner, so the worker cannot use it.
 * * **Read-write token** — `BLOB_READ_WRITE_TOKEN`. Long-lived and static, and what Vercel's own
 *   documentation points to for code running outside Vercel, such as a CI job. It is scoped to one
 *   store, which is why it is the narrower credential here despite being the static one: the
 *   alternative for CI is a Vercel account token, which can reach the whole account.
 *
 * Resolved in the same order as the SDK's own resolver, so this never disagrees with it. Returned
 * as options to spread into a call rather than as a bare string, because the two credentials are
 * passed under different names.
 */
function credentials() {
  const rw = process.env.BLOB_READ_WRITE_TOKEN?.trim();
  if (rw) return { token: rw };

  const oidcToken = process.env.VERCEL_OIDC_TOKEN?.trim();
  const storeId = process.env.BLOB_STORE_ID?.trim();
  if (oidcToken && storeId) return { oidcToken, storeId };
  if (oidcToken) {
    fail("VERCEL_OIDC_TOKEN is set but BLOB_STORE_ID is not; OIDC needs the store it names", 1);
  }
  if (storeId) {
    fail("BLOB_STORE_ID is set but VERCEL_OIDC_TOKEN is not. Outside Vercel there is no OIDC "
         + "token to pair it with; set BLOB_READ_WRITE_TOKEN instead", 1);
  }
  fail("no Blob credentials: set BLOB_READ_WRITE_TOKEN (outside Vercel, including CI), or "
       + "VERCEL_OIDC_TOKEN with BLOB_STORE_ID (on Vercel)", 1);
}

/** Which credential is in use, for a message. Never the credential itself. */
function credentialKind(creds) {
  return creds.token ? "read-write token" : "oidc";
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
    return await head(key, { ...credentials() });
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
    ...credentials(),
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
  const found = await get(o.key, { access: ACCESS, ...credentials(), useCache: false });
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
      ...credentials(), prefix: o.prefix || undefined, limit: 1000, cursor,
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
  await del(o.key, { ...credentials() });
  emit({ deleted: o.key });
}

/**
 * Does the configured credential actually open this store? Reads one object listing and writes
 * nothing.
 *
 * It exists so the credential can be proved before a run that writes. The failure it is aimed at is
 * not a typo — a wrong token fails loudly on the first call either way — but the run that gets far
 * enough to matter before it fails: the worker restores a dataset, spends ten minutes refreshing
 * sources and rendering, and only then discovers it cannot save. A list is the cheapest call that
 * still requires the credential to be valid for this store.
 *
 * The store id is reported only when OIDC names it in the environment. Deriving it from a
 * read-write token means splitting the token on an undocumented internal format, and the SDK does
 * not export its parser; that is exactly the guesswork this helper exists to avoid.
 */
async function cmdCheck() {
  const creds = credentials();
  const page = await list({ ...creds, limit: 1 });
  emit({
    credential: credentialKind(creds),
    store_id: creds.storeId ?? null,
    reachable: true,
    objects_seen: page.blobs.length,
    store_has_more: Boolean(page.hasMore),
  });
}

const COMMANDS = {
  put: cmdPut, get: cmdGet, head: cmdHead, list: cmdList, del: cmdDel, check: cmdCheck,
};

const [command, ...rest] = process.argv.slice(2);
// Imported rather than executed — by a test reaching for `advice` — so there is no command line to
// read and nothing to run.
const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
const run = invoked ? COMMANDS[command] : null;
if (invoked && !run) {
  fail(`unknown command ${command ?? "(none)"}; expected one of ${Object.keys(COMMANDS).join(", ")}`);
}

try {
  if (run) await run(args(rest));
} catch (error) {
  // The message can carry a pathname but never the credential, which the SDK keeps in a header.
  fail(`${error?.name ?? "Error"}: ${error?.message ?? String(error)}${advice(error)}`, 1);
}

/**
 * The one failure whose message names the symptom and not the fix.
 *
 * `OIDC is enabled for this project, but not for the "development" environment` is accurate and
 * unhelpful: it reads as though OIDC needs enabling somewhere, when what it means is that the
 * *store* is connected to some environments and not to the one this token was issued for. A token
 * minted for a runner is always a development one — preview and production tokens are issued to
 * deployments at runtime, not on demand — so a store connected only to Preview and Production
 * refuses every call from CI with this message.
 */
export function advice(error) {
  const message = String(error?.message ?? "");
  if (!message.startsWith("Vercel Blob: OIDC is enabled for this project")) return "";
  const environment = /for the "([^"]+)" environment/.exec(message)?.[1] ?? "this token's";
  return `\n\nThe store is not connected to the ${environment} environment, which is the one this`
    + " token was issued for. A credential minted outside a deployment is always a development"
    + " one, so a store connected only to Preview and Production cannot be reached from CI."
    + "\nEither add that environment to the store's project connection (Storage -> the store ->"
    + " Projects -> \u22ef -> Update Project Connection), or set BLOB_READ_WRITE_TOKEN, which"
    + " carries its own store and no environment at all.";
}
