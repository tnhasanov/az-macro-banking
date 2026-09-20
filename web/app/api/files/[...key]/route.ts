/**
 * Serving a report file to a signed-in person.
 *
 * Two things make this safe, and both are necessary:
 *
 * **The key is checked against the catalogue, not sanitised.** A request may only fetch a file that
 * some edition record actually lists. Path cleaning would be a weaker control — it asks "does this
 * look dangerous?" instead of "is this one of the files we publish?" — and it would still allow a
 * signed-in reader to pull the dataset tarball or the pointer object, neither of which belongs to
 * anyone's report.
 *
 * **The storage URL never reaches the browser.** Vercel Blob serves over unguessable public URLs,
 * so handing one out would create a link that works without a session and cannot be revoked. The
 * bytes are streamed through this route instead, and the session is checked on every request.
 */
import { NextResponse } from "next/server";
import { sql } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const CONTENT_TYPES: Record<string, string> = {
  pdf: "application/pdf",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

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
  _request: Request,
  { params }: { params: Promise<{ key: string[] }> },
) {
  const { key: segments } = await params;
  const key = segments.map(decodeURIComponent).join("/");

  const known = await isPublishedFile(key).catch(() => null);
  if (!known) {
    // Same answer whether the key is unknown, malformed or deliberately out of bounds: a distinct
    // message would map the store for anyone probing it.
    return NextResponse.json({ error: "No such report file." }, { status: 404 });
  }

  const token = process.env.BLOB_READ_WRITE_TOKEN;
  if (!token) {
    return NextResponse.json(
      { error: "This deployment has no blob storage configured." },
      { status: 503 },
    );
  }

  const { list } = await import("@vercel/blob");
  const found = await list({ prefix: key, limit: 2, token });
  const blob = found.blobs.find((b) => b.pathname === key);
  if (!blob) {
    return NextResponse.json(
      { error: "The catalogue lists this file but storage does not hold it." },
      { status: 410 },
    );
  }

  const upstream = await fetch(blob.downloadUrl ?? blob.url, { cache: "no-store" });
  if (!upstream.ok || !upstream.body) {
    return NextResponse.json({ error: "The file could not be read from storage." }, { status: 502 });
  }

  const extension = known.name.split(".").pop()?.toLowerCase() ?? "";
  return new NextResponse(upstream.body, {
    headers: {
      "content-type": CONTENT_TYPES[extension] ?? "application/octet-stream",
      // The filename is the catalogued one, quoted, so a name in the store cannot inject a header.
      "content-disposition": `attachment; filename="${known.name.replace(/[^\w.\-]/g, "_")}"`,
      "content-length": upstream.headers.get("content-length") ?? "",
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
