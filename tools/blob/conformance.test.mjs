/**
 * Does the real SDK, driven by our options, put the right thing on the wire?
 *
 * **This is not an integration test and must never be reported as one.** Vercel is unreachable from
 * this environment, so undici's MockAgent answers the requests. Only the transport is substituted:
 * every request below is constructed by the real @vercel/blob from the same options `blob.mjs`
 * passes, so the access level, the credential, the API version, the pathname parameter and the
 * multipart decision are observed rather than assumed.
 *
 * What it cannot show is whether Vercel agrees with any of it. Only a real store can.
 *
 * It exists because reading the SDK's source found a defect that no amount of local testing against
 * our own abstraction would have: `multipart` is opt-in. We were not passing it, so a 260 MB upload
 * was a single streamed request with no per-part retry — the opposite of the reason we adopted the
 * SDK in the first place.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { MockAgent, setGlobalDispatcher } from "undici";

const TOKEN = "vercel_blob_rw_teststore_conformanceonly";
const STORE_ID = TOKEN.split("_")[3];
const API = "https://vercel.com";
const DOWNLOAD = `https://${STORE_ID}.private.blob.vercel-storage.com`;

/** Every request the SDK made, so assertions are about real traffic. */
let seen = [];
let store = new Map();

function install() {
  seen = [];
  store = new Map();
  const agent = new MockAgent();
  agent.disableNetConnect();
  setGlobalDispatcher(agent);

  const api = agent.get(API);
  api.intercept({ path: (p) => p.startsWith("/api/blob"), method: "PUT" }).reply((req) => {
    seen.push({ method: "PUT", path: req.path, headers: req.headers });
    const pathname = new URL(req.path, API).searchParams.get("pathname");
    store.set(pathname, Buffer.from(req.body ?? ""));
    return { statusCode: 200, data: { url: `${DOWNLOAD}/${pathname}`, pathname,
                                      contentType: req.headers["x-content-type"] ?? null } };
  }).persist();

  api.intercept({ path: (p) => p.startsWith("/api/blob"), method: "GET" }).reply((req) => {
    seen.push({ method: "GET", path: req.path, headers: req.headers });
    const params = new URL(req.path, API).searchParams;
    const url = params.get("url");
    if (url !== null) {
      const key = url.startsWith("http") ? new URL(url).pathname.slice(1) : url;
      if (!store.has(key)) return { statusCode: 404, data: { error: { code: "not_found" } } };
      return { statusCode: 200, data: { url: `${DOWNLOAD}/${key}`, pathname: key,
                                        size: store.get(key).length, contentType: null,
                                        uploadedAt: new Date().toISOString(), etag: "e" } };
    }
    const prefix = params.get("prefix") ?? "";
    return { statusCode: 200, data: {
      blobs: [...store.keys()].filter((k) => k.startsWith(prefix)).map((k) => ({
        url: `${DOWNLOAD}/${k}`, pathname: k, size: store.get(k).length,
        uploadedAt: new Date().toISOString() })),
      hasMore: false, cursor: null } };
  }).persist();

  // Multipart: create -> upload parts -> complete, all on /api/blob/mpu, distinguished by
  // x-mpu-action. Without these the SDK's multipart path cannot be exercised at all.
  const parts = new Map();
  api.intercept({ path: (p) => p.startsWith("/api/blob/mpu"), method: "POST" }).reply((req) => {
    const action = req.headers["x-mpu-action"];
    const pathname = new URL(req.path, API).searchParams.get("pathname");
    seen.push({ method: `MPU:${action}`, path: req.path, headers: req.headers });
    if (action === "create") {
      parts.set(pathname, []);
      return { statusCode: 200, data: { uploadId: "u1", key: `k-${pathname}` } };
    }
    if (action === "upload") {
      const n = Number(req.headers["x-mpu-part-number"]);
      parts.get(pathname).push({ partNumber: n, size: (req.body ?? "").length });
      return { statusCode: 200, data: { etag: `e${n}`, partNumber: n } };
    }
    // complete
    const total = (parts.get(pathname) ?? []).reduce((a, p) => a + p.size, 0);
    store.set(pathname, Buffer.alloc(total));
    return { statusCode: 200, data: { url: `${DOWNLOAD}/${pathname}`, pathname, contentType: null } };
  }).persist();

  agent.get(DOWNLOAD).intercept({ path: () => true, method: "GET" }).reply((req) => {
    seen.push({ method: "DOWNLOAD", path: req.path, headers: req.headers });
    if (!String(req.headers.authorization || "").startsWith("Bearer ")) {
      return { statusCode: 401, data: "" };
    }
    const key = decodeURIComponent(req.path.split("?")[0].slice(1));
    if (!store.has(key)) return { statusCode: 404, data: "" };
    return { statusCode: 200, data: store.get(key) };
  }).persist();
}

/** The options `blob.mjs` passes, kept here so the two cannot drift silently. */
const PUT_OPTIONS = (size) => ({
  access: "private",
  token: TOKEN,
  contentType: "application/gzip",
  addRandomSuffix: false,
  allowOverwrite: false,
  multipart: size > 8 * 1024 * 1024,
  cacheControlMaxAge: 0,
});

test("an upload is addressed by pathname and marked private", async () => {
  install();
  const { put } = await import("@vercel/blob");
  await put("dataset/state-x.tar.gz", Buffer.alloc(1024), PUT_OPTIONS(1024));

  const req = seen.find((s) => s.method === "PUT");
  assert.ok(req, "no PUT reached the wire");
  assert.match(req.path, /^\/api\/blob/, "the API base is vercel.com/api/blob");
  assert.equal(new URL(req.path, API).searchParams.get("pathname"), "dataset/state-x.tar.gz");
  assert.equal(req.headers["x-vercel-blob-access"], "private",
    "every object must be written private; a public object has an unrevokable URL");
});

test("the credential travels in a header, never in the URL", async () => {
  install();
  const { put } = await import("@vercel/blob");
  await put("dataset/state-y.tar.gz", Buffer.alloc(64), PUT_OPTIONS(64));

  const req = seen.find((s) => s.method === "PUT");
  assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
  assert.ok(!req.path.includes(TOKEN), "a token in a URL ends up in a log");
});

test("the SDK sends its own API version, which we do not hardcode", async () => {
  install();
  const { put } = await import("@vercel/blob");
  await put("dataset/state-z.tar.gz", Buffer.alloc(64), PUT_OPTIONS(64));

  const version = seen.find((s) => s.method === "PUT").headers["x-api-version"];
  assert.ok(Number.parseInt(version, 10) >= 12,
    `expected the installed SDK's version header, got ${version}`);
});

test("the threshold decides multipart, and the client and this test agree on it", async () => {
  // The wire behaviour of the multipart path cannot be exercised here: it does not route through
  // the dispatcher MockAgent installs, so the request never reaches the interceptor. What can be
  // pinned is the decision, which is where the defect was — `multipart` is opt-in, and we were
  // not passing it at all, so a 260 MB upload was one streamed request with no per-part retry.
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("./blob.mjs", import.meta.url), "utf8");

  const declared = source.match(/const MULTIPART_THRESHOLD = ([^;]+);/);
  assert.ok(declared, "blob.mjs no longer declares a multipart threshold");
  // eslint-disable-next-line no-eval
  const threshold = eval(declared[1]);
  assert.equal(threshold, 8 * 1024 * 1024,
    "the SDK's part size is 8 MB, so a smaller threshold cannot produce more than one part");

  assert.ok(/multipart = size > MULTIPART_THRESHOLD/.test(source),
    "the decision must come from the size of the file being uploaded");
  assert.ok(/^\s*multipart,$/m.test(source), "and it must actually be passed to put()");

  // The two sizes that matter in production, against the same expression the client uses.
  assert.equal(5.9 * 1024 * 1024 > threshold, false, "the dataset's mutable half stays single-request");
  assert.equal(254 * 1024 * 1024 > threshold, true, "the dataset's static half is uploaded in parts");
});

test("a download is an authenticated GET on the private host", async () => {
  install();
  const { put, get } = await import("@vercel/blob");
  await put("reports/m/v1/a.pdf", Buffer.from("%PDF-1.7\n"), PUT_OPTIONS(9));

  seen = [];
  const found = await get("reports/m/v1/a.pdf", { access: "private", token: TOKEN, useCache: false });
  assert.ok(found?.stream, "a private read must return a stream");

  const req = seen.find((s) => s.method === "DOWNLOAD");
  assert.ok(req, "the download did not reach the private host");
  assert.equal(req.headers.authorization, `Bearer ${TOKEN}`,
    "this is the defect that started all of it: the download used to carry no credential");
});

test("a missing object is a typed absence, matched by class and not by name", async () => {
  install();
  const { head, BlobNotFoundError } = await import("@vercel/blob");

  const error = await head("reports/nope.pdf", { token: TOKEN }).then(() => null, (e) => e);
  assert.ok(error, "head must reject for a blob that is not there");
  assert.ok(error instanceof BlobNotFoundError, "the client maps this to exit 3");

  // The defect this pins. `BlobNotFoundError` does not set `.name`, so an instance reports
  // "Error"; only `constructor.name` says otherwise. A check on `error.name` is always false,
  // which turned every "is this key already there?" into a thrown storage failure — and
  // `publish_edition` asks exactly that before writing anything, so the first upload of every
  // edition would have failed.
  assert.notEqual(error.name, "BlobNotFoundError",
    "if the SDK ever sets .name, this test should be revisited rather than silently passing");
  assert.equal(error.constructor.name, "BlobNotFoundError");
});

test("the client matches the SDK's not-found the same way this test does", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("./blob.mjs", import.meta.url), "utf8");
  assert.ok(source.includes("error instanceof BlobNotFoundError"),
    "blob.mjs must match by class; a `.name` comparison silently never fires");
  assert.ok(!source.includes('name === "BlobNotFoundError"'),
    "the duck-typed check must not come back");
});

// --------------------------------------------------------------- the two credentials

/**
 * A private store takes either a read-write token or OIDC, and the choice is made by where the code
 * runs, not by preference. The worker runs on a GitHub Actions runner, where no Vercel-issued OIDC
 * token exists and a read-write token is the only option Vercel documents; the dashboard runs on
 * Vercel, where a connected store supplies OIDC and no static token is present at all.
 *
 * `blob.mjs` therefore resolves both, in the SDK's own order. These tests put each on the wire,
 * because the two are passed under different option names and a mistake in either is invisible
 * until a real request is refused.
 */
const OIDC_TOKEN = "ey.conformance.oidc";

test("a read-write token authenticates as a bearer credential", async () => {
  install();
  const { list } = await import("@vercel/blob");
  await list({ token: TOKEN, limit: 1 });

  const req = seen.find((s) => s.method === "GET");
  assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
});

test("OIDC authenticates with the oidc token and the store it names", async () => {
  install();
  const { list } = await import("@vercel/blob");
  await list({ oidcToken: OIDC_TOKEN, storeId: STORE_ID, limit: 1 });

  const req = seen.find((s) => s.method === "GET");
  assert.equal(req.headers.authorization, `Bearer ${OIDC_TOKEN}`,
    "the OIDC token is the credential; there is no read-write token to fall back to");
  assert.ok(!JSON.stringify(req).includes(TOKEN),
    "no read-write token may appear on an OIDC request");
});

test("the store id is accepted with or without its prefix", async () => {
  install();
  const { list } = await import("@vercel/blob");
  await list({ oidcToken: OIDC_TOKEN, storeId: `store_${STORE_ID}`, limit: 1 });
  assert.ok(seen.find((s) => s.method === "GET"), "a prefixed store id is not rejected");
});

test("OIDC without a store id is refused rather than guessed", async () => {
  install();
  const { list } = await import("@vercel/blob");
  await assert.rejects(
    () => list({ oidcToken: OIDC_TOKEN, limit: 1 }),
    /storeId/,
    "an OIDC token names no store by itself, and the SDK will not invent one",
  );
});

test("both credentials reach the private download too", async () => {
  install();
  const { put, get } = await import("@vercel/blob");
  await put("reports/cred.pdf", Buffer.alloc(32), { ...PUT_OPTIONS(32), contentType: "application/pdf" });

  for (const [label, creds] of [
    ["read-write", { token: TOKEN }],
    ["oidc", { oidcToken: OIDC_TOKEN, storeId: STORE_ID }],
  ]) {
    seen = [];
    const found = await get("reports/cred.pdf", { access: "private", ...creds, useCache: false });
    assert.ok(found?.stream, `${label} could not read a private blob`);
    await new Response(found.stream).arrayBuffer();
    const download = seen.find((s) => s.method === "DOWNLOAD");
    assert.ok(String(download.headers.authorization).startsWith("Bearer "),
      `${label}: a private download is refused without a credential`);
  }
});

// ------------------------------------------ the store's environments vs the token's

/**
 * The refusal that reads as the wrong problem.
 *
 * `OIDC is enabled for this project, but not for the "development" environment` is accurate and
 * gives no hint that what needs changing is a *store connection*. A credential minted outside a
 * deployment is always a development one — preview and production tokens are issued to deployments
 * at runtime, not on demand — so a store connected only to Preview and Production refuses every
 * call from CI with exactly this message.
 */
test("the SDK raises its own error type for an environment mismatch", async () => {
  install();
  const { list, BlobError } = await import("@vercel/blob");

  const agent = new MockAgent();
  agent.disableNetConnect();
  setGlobalDispatcher(agent);
  agent.get(API).intercept({ path: (p) => p.startsWith("/api/blob"), method: "GET" })
    .reply(403, { error: { code: "forbidden",
      message: 'OIDC is enabled for this project, but not for the "development" environment.' } })
    .persist();

  await assert.rejects(
    () => list({ oidcToken: OIDC_TOKEN, storeId: STORE_ID, limit: 1 }),
    (error) => {
      assert.ok(error instanceof BlobError);
      assert.match(error.message, /not for the "development" environment/);
      return true;
    },
  );
});

test("the helper turns that message into the change that fixes it", async () => {
  // Run against the real wording the SDK produces, not a paraphrase of it: the helper matches on
  // that message, so a reworded one would silently stop being explained.
  const { advice } = await import("./blob.mjs?advice");
  const said = advice(new Error(
    'Vercel Blob: OIDC is enabled for this project, but not for the "development" environment.'));

  assert.match(said, /not connected to the development environment/,
    "it must name the environment out of the message, not a guess");
  assert.match(said, /Update Project Connection/, "the fix is a store connection; say where");
  assert.match(said, /BLOB_READ_WRITE_TOKEN/, "and name the credential that has no environment");
});

test("nothing else gets unsolicited advice", async () => {
  const { advice } = await import("./blob.mjs?advice");
  assert.equal(advice(new Error("Vercel Blob: Access denied, please provide a valid token")), "");
  assert.equal(advice(new Error("ENOTFOUND")), "");
});
