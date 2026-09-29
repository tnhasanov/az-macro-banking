/**
 * Serving a report file to a signed-in person, out of private storage.
 *
 * Four things make this safe, and all four are necessary:
 *
 * **The session is checked here, not only in middleware.** The middleware matcher is one regex, and
 * a route that serves a bank's reports should not depend on nobody ever editing it carelessly. The
 * check below is cheap and it is the one that matters.
 *
 * **The key is checked against the catalogue, not sanitised.** A request may only fetch a file that
 * some edition record actually lists. Path cleaning would be a weaker control — it asks "does this
 * look dangerous?" instead of "is this one of the files we publish?" — and it would still allow a
 * signed-in reader to pull the dataset tarball, the delivery ledger or a raw source document, none
 * of which belongs to anyone's report.
 *
 * **The blob is private and read with a credential.** Objects are written with `access: 'private'`,
 * so there is no publicly readable URL to leak in the first place. The previous version fetched
 * `blob.downloadUrl` with no credentials, which only ever worked against a public store — exactly
 * the arrangement this deployment must not have.
 *
 * **The token never reaches the browser.** The bytes are streamed through this route. Nothing that
 * leaves the server can be replayed by someone who is not signed in.
 */
import { NextResponse } from "next/server";
import { readSession, SESSION_COOKIE } from "@/lib/auth";
import { sql } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const CONTENT_TYPES: Record<string, string> = {
  pdf: "application/pdf",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

/** Only these leave the building. The dataset, the ledger and raw sources are not report files. */
const SERVABLE = new Set(Object.keys(CONTENT_TYPES));

/** Is this exactly a file that some edition publishes? */
async function isPublishedFile(key: string): Promise<{ name: string } | null> {
  const rows = await sql()<{ name: string }[]>`
    SELECT f->>'name' AS name
    FROM editions e, jsonb_array_elements(e.files) AS f
    WHERE f->>'key' = ${key}
    LIMIT 1`;
  return rows[0] ?? null;
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ key: string[] }> },
) {
  const cookie = request.headers.get("cookie") ?? "";
  const token = cookie.split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith(`${SESSION_COOKIE}=`))
    ?.slice(SESSION_COOKIE.length + 1);
  if (!(await readSession(token))) {
    return NextResponse.json({ error: "not signed in" }, { status: 401 });
  }

  const { key: segments } = await params;
  const key = segments.map(decodeURIComponent).join("/");
  const extension = key.split(".").pop()?.toLowerCase() ?? "";

  // Same answer whether the key is unknown, malformed, of a type this route does not serve, or
  // deliberately out of bounds: a distinct message would map the store for anyone probing it.
  const known = SERVABLE.has(extension) ? await isPublishedFile(key).catch(() => null) : null;
  if (!known) {
    return NextResponse.json({ error: "No such report file." }, { status: 404 });
  }

  // Local integration only: the worker wrote to a directory, so the dashboard reads that directory.
  // Never in production, where the only store is the private Blob store.
  const local = await (await import("@/lib/blob")).localDownload(key, known.name, CONTENT_TYPES[extension]);
  if (local) return local;

  // Two credentials open a private store, and which one this deployment has depends on how the
  // store was connected. Connecting it to a project gives the project OIDC: Vercel injects a
  // short-lived, auto-rotating token and `BLOB_STORE_ID` naming the store, and the SDK pairs them
  // without being asked. A read-write token is the static alternative, and the only one available
  // to code running outside Vercel — the GitHub Actions worker uses it, this route need not.
  //
  // So the token is passed only when there is one. Passing `token: undefined` would resolve the
  // same way, but being explicit keeps it visible that omitting it is what selects OIDC rather
  // than an oversight.
  const blobToken = process.env.BLOB_READ_WRITE_TOKEN;
  if (!blobToken && !process.env.BLOB_STORE_ID) {
    return NextResponse.json(
      { error: "This deployment has no blob storage configured." },
      { status: 503 },
    );
  }

  const { get } = await import("@vercel/blob");
  let found;
  try {
    found = await get(key, {
      access: "private",
      useCache: false,
      ...(blobToken ? { token: blobToken } : {}),
    });
  } catch (error) {
    // The SDK's message can name the pathname; it never carries the token.
    const message = error instanceof Error ? error.name : "unknown";
    return NextResponse.json(
      { error: "The file could not be read from storage.", detail: message },
      { status: 502 },
    );
  }

  if (!found?.stream) {
    return NextResponse.json(
      { error: "The catalogue lists this file but storage does not hold it." },
      { status: 410 },
    );
  }

  return new NextResponse(found.stream as unknown as ReadableStream, {
    headers: {
      "content-type": CONTENT_TYPES[extension],
      // The filename is the catalogued one, sanitised, so a name in the store cannot inject a header.
      "content-disposition": `attachment; filename="${known.name.replace(/[^\w.\-]/g, "_")}"`,
      ...(found.blob?.size ? { "content-length": String(found.blob.size) } : {}),
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
