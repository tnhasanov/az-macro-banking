/**
 * Serving a report file out of private storage.
 *
 * The defect this guards: the route fetched `blob.downloadUrl` with a plain `fetch` and no
 * credentials. That only works against a *public* blob store — which is exactly the arrangement
 * this deployment must not have, because a public URL is a permanent, unrevokable, unauthenticated
 * link to a bank's report.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROUTE = readFileSync(new URL("../app/api/files/[...key]/route.ts", import.meta.url), "utf8");
const STORE = readFileSync(
  new URL("../../azmonitor/cloud/objectstore.py", import.meta.url), "utf8");
const HELPER = readFileSync(new URL("../../tools/blob/blob.mjs", import.meta.url), "utf8");

test("the download is an authenticated read of a private blob", () => {
  assert.ok(ROUTE.includes('access: "private"'), "the blob must be read as private");
  assert.ok(ROUTE.includes("token: blobToken"), "the read must carry a credential");
});

/** The file with comments removed, so a test about code does not match the prose describing it. */
function code(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n").map((l) => l.replace(/\/\/.*$/, "")).join("\n");
}

test("no unauthenticated fetch of a storage URL survives", () => {
  const body = code(ROUTE);
  assert.ok(!body.includes("downloadUrl"),
    "downloadUrl is a public-store concept; a private blob has no URL worth handing out");
  assert.ok(!body.includes("fetch("),
    "nothing here should fetch a storage URL directly; the SDK does the authenticated read");
});

test("the token never reaches the browser", () => {
  const response = ROUTE.slice(ROUTE.indexOf("return new NextResponse"));
  assert.ok(!response.includes("blobToken") && !response.includes("token"),
    "no response header or body may carry the token");
  assert.ok(ROUTE.includes("found.stream"), "the bytes are streamed through this route");
});

test("the session is checked in the handler, not only in middleware", () => {
  assert.ok(ROUTE.includes("readSession"),
    "a route serving a bank's reports must not depend on one regex in a matcher");
  assert.ok(ROUTE.indexOf("readSession") < ROUTE.indexOf("isPublishedFile"),
    "the session check comes first");
});

test("only catalogued report files can be served", () => {
  assert.ok(ROUTE.includes("isPublishedFile"), "membership of the catalogue is the authorisation");
  assert.ok(ROUTE.includes("SERVABLE"), "the extension allowlist keeps non-report objects out");
  // The dataset, the delivery ledger and raw source documents are none of these.
  for (const ext of ["pdf", "pptx", "xlsx"]) assert.ok(ROUTE.includes(`${ext}:`));
  for (const forbidden of ["tar.gz", "sqlite", "json"]) {
    assert.ok(!new RegExp(`SERVABLE[^)]*${forbidden}`).test(ROUTE),
      `${forbidden} must not be servable through the report route`);
  }
});

test("an unknown, wrong-typed or out-of-bounds key all answer the same", () => {
  const notFound = (ROUTE.match(/No such report file\./g) || []).length;
  assert.equal(notFound, 1, "one message, one branch: probing must not map the store");
});

test("the python store writes every object privately", () => {
  assert.ok(HELPER.includes('const ACCESS = "private"'));
  assert.ok(/access: ACCESS/.test(HELPER), "put and get must both use it");
  assert.ok(!STORE.includes("urllib.request"),
    "the hand-rolled REST client is gone; the official SDK handles the protocol");
});

test("the python store hands out no URL at all", () => {
  const urlFor = STORE.slice(STORE.indexOf("class VercelBlobStore"));
  const fn = urlFor.slice(urlFor.indexOf("def url_for"), urlFor.indexOf("def url_for") + 400);
  assert.ok(fn.includes("return None"),
    "a private blob has no URL that works without the token, so there is nothing to return");
});

test("an interrupted download leaves nothing that looks complete", () => {
  assert.ok(HELPER.includes(".partial"), "a download is staged under a temporary name");
  assert.ok(HELPER.includes("await rename("), "and moved into place only when whole");
  assert.ok(HELPER.includes("was truncated"), "a short read is an error, not a shorter file");
});

test("the helper distinguishes missing from refused from broken", () => {
  assert.ok(HELPER.includes("const NOT_FOUND = 3"));
  assert.ok(HELPER.includes("const REFUSED = 4"));
  assert.ok(STORE.includes("NOT_FOUND = 3") && STORE.includes("REFUSED = 4"),
    "and Python reads the same codes");
});
