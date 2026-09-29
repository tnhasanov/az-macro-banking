/**
 * A stand-in for the Vercel Blob service, kept in a local directory, for rehearsing the seed on a
 * machine that has no store — the Windows job in CI is what it is for.
 *
 * **It is never loaded by `blob.mjs`.** It is preloaded explicitly, with
 * `NODE_OPTIONS=--import=<file URL of this file>` and `AZMONITOR_FAKE_BLOB_DIR` set, so every helper
 * process the Python engine starts talks to it instead of the network. What that proves is the
 * whole path on the machine it runs on — Python starting Node, the helper's argument handling and
 * file moves, the SDK building each request — while the service's answers come from here. It
 * proves nothing about Vercel itself, and must never be reported as a real upload.
 *
 * Downloads are answered the way the real service was observed to answer them: a small object plain
 * with a Content-Length, a larger one Brotli-compressed with no Content-Length and a weak ETag. The
 * helper once took the missing length for a zero-byte object and called every archive truncated.
 *
 * Two limits, both deliberate:
 *   * It only accepts the rehearsal token (`vercel_blob_rw_rehearsal_…`), so a real credential can
 *     never be pointed at it by mistake and then sent somewhere else.
 *   * Bodies above the helper's 8 MB multipart threshold are not covered: the SDK's multipart path
 *     does not go through the dispatcher installed here (see conformance.test.mjs), so such an
 *     upload would reach vercel.com with the rehearsal token and be refused. Rehearsal bundles stay
 *     below it.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { brotliCompressSync } from "node:zlib";

const ROOT = process.env.AZMONITOR_FAKE_BLOB_DIR;
const TOKEN = process.env.BLOB_READ_WRITE_TOKEN?.trim() ?? "";
if (!ROOT) throw new Error("fake-store.mjs needs AZMONITOR_FAKE_BLOB_DIR");
if (TOKEN && !/^vercel_blob_rw_rehearsal_[A-Za-z0-9]+$/.test(TOKEN)) {
  throw new Error("fake-store.mjs only accepts the rehearsal token (vercel_blob_rw_rehearsal_…)");
}

const API = "https://vercel.com";
const DOWNLOAD = "https://rehearsal.private.blob.vercel-storage.com";
const OBJECTS = join(ROOT, "objects");
/** The real service sent a 724-byte blob plain and a 5.8 MB one compressed; this sits between. */
const COMPRESS_OVER = 16 * 1024;

const file = (pathname) => join(OBJECTS, encodeURIComponent(pathname));
const etagOf = (bytes) => `"${createHash("sha256").update(bytes).digest("hex").slice(0, 32)}"`;
const keyOf = (u) => (u.startsWith("http") ? decodeURIComponent(new URL(u).pathname.slice(1)) : u);

function meta(pathname) {
  const path = file(pathname);
  if (!existsSync(path)) return null;
  const bytes = readFileSync(path);
  const info = JSON.parse(readFileSync(`${path}.meta`, "utf8"));
  return { url: `${DOWNLOAD}/${pathname}`, downloadUrl: `${DOWNLOAD}/${pathname}?download=1`,
           pathname, size: bytes.length, contentType: info.contentType, contentDisposition: "",
           cacheControl: "max-age=0", uploadedAt: info.uploadedAt, etag: etagOf(bytes) };
}

function allKeys() {
  return readdirSync(OBJECTS).filter((n) => !n.endsWith(".meta")).map(decodeURIComponent).sort();
}

async function bodyOf(body) {
  if (body == null) return Buffer.alloc(0);
  if (typeof body === "string" || body instanceof Uint8Array) return Buffer.from(body);
  const chunks = [];
  for await (const chunk of body) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

const refuse = (statusCode, code, message) => ({ statusCode, data: { error: { code, message } } });
const bearer = (req) => req.headers.authorization === `Bearer ${TOKEN}`;

async function install() {
  const { MockAgent, setGlobalDispatcher } = await import("undici");
  mkdirSync(OBJECTS, { recursive: true });
  const agent = new MockAgent();
  agent.disableNetConnect();
  setGlobalDispatcher(agent);
  const api = agent.get(API);

  api.intercept({ path: (p) => p.startsWith("/api/blob"), method: "PUT" }).reply((req) => {
    if (!bearer(req)) return refuse(403, "forbidden", "Access denied");
    const pathname = new URL(req.path, API).searchParams.get("pathname");
    const current = meta(pathname);
    const ifMatch = req.headers["x-if-match"];
    if (ifMatch && (!current || current.etag !== ifMatch)) {
      return refuse(412, "precondition_failed", "The blob has changed");
    }
    if (current && req.headers["x-allow-overwrite"] !== "1") {
      return refuse(400, "bad_request", "This blob already exists");
    }
    const contentType = req.headers["x-content-type"] ?? "application/octet-stream";
    return {
      statusCode: 200,
      data: async () => {
        const bytes = await bodyOf(req.body);
        writeFileSync(file(pathname), bytes);
        writeFileSync(`${file(pathname)}.meta`,
                      JSON.stringify({ contentType, uploadedAt: new Date().toISOString() }));
        const m = meta(pathname);
        return { url: m.url, downloadUrl: m.downloadUrl, pathname, contentType,
                 contentDisposition: "", etag: m.etag };
      },
    };
  }).persist();

  api.intercept({ path: (p) => p.startsWith("/api/blob/delete"), method: "POST" }).reply((req) => {
    if (!bearer(req)) return refuse(403, "forbidden", "Access denied");
    for (const u of JSON.parse(String(req.body)).urls) {
      rmSync(file(keyOf(u)), { force: true });
      rmSync(`${file(keyOf(u))}.meta`, { force: true });
    }
    return { statusCode: 200, data: {} };
  }).persist();

  api.intercept({ path: (p) => p.startsWith("/api/blob"), method: "GET" }).reply((req) => {
    if (!bearer(req)) return refuse(403, "forbidden", "Access denied");
    const params = new URL(req.path, API).searchParams;
    const url = params.get("url");
    if (url !== null) {                                    // head
      const m = meta(keyOf(url));
      return m ? { statusCode: 200, data: m } : refuse(404, "not_found", "The requested blob does not exist");
    }
    const prefix = params.get("prefix") ?? "";            // list, in one page
    const limit = Number(params.get("limit") ?? 1000);
    const keys = allKeys().filter((k) => k.startsWith(prefix));
    const blobs = keys.slice(0, limit).map((k) => {
      const m = meta(k);
      return { url: m.url, downloadUrl: m.downloadUrl, pathname: k, size: m.size,
               uploadedAt: m.uploadedAt, etag: m.etag };
    });
    return { statusCode: 200, data: { blobs, hasMore: keys.length > limit, cursor: null } };
  }).persist();

  agent.get(DOWNLOAD).intercept({ path: () => true, method: "GET" }).reply((req) => {
    if (!bearer(req)) return { statusCode: 403, data: "" };
    const key = decodeURIComponent(req.path.split("?")[0].slice(1));
    const m = meta(key);
    if (!m) return { statusCode: 404, data: "" };
    const bytes = readFileSync(file(key));
    if (bytes.length > COMPRESS_OVER) {
      // As the real service answers a larger private blob (observed 2026-09-28): Brotli, chunked,
      // no Content-Length, and the weak form of the ETag.
      return { statusCode: 200, data: brotliCompressSync(bytes),
               responseOptions: { headers: { "content-encoding": "br", etag: `W/${m.etag}`,
                                             "content-type": m.contentType } } };
    }
    return { statusCode: 200, data: bytes,
             responseOptions: { headers: { "content-length": String(bytes.length), etag: m.etag,
                                           "content-type": m.contentType } } };
  }).persist();
}

// NODE_OPTIONS reaches every Node process, npm included, and npm may run before tools/blob has its
// dependencies. Only a process holding the token is a Blob helper; everything else is left alone.
if (TOKEN) await install();
