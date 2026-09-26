/**
 * Reading one private object on the server. The same credential rules as the download route: a
 * static read-write token when the deployment has one, otherwise the project's OIDC connection.
 * The bytes stay on the server; nothing here produces a URL anyone else could use.
 */
export async function readPrivate(key: string, maxBytes: number): Promise<Buffer | null> {
  const token = process.env.BLOB_READ_WRITE_TOKEN;
  if (process.env.AZMONITOR_OBJECT_STORE_DIR && process.env.VERCEL_ENV !== "production") {
    // Local integration: the worker wrote to a directory, so the web reads the same directory.
    const { readFile, stat } = await import("node:fs/promises");
    const path = await import("node:path");
    const root = path.resolve(process.env.AZMONITOR_OBJECT_STORE_DIR);
    const file = path.resolve(root, key);
    if (!file.startsWith(root + path.sep)) return null;
    const info = await stat(file).catch(() => null);
    if (!info || info.size > maxBytes) return null;
    return readFile(file);
  }
  if (!token && !process.env.BLOB_STORE_ID) return null;
  const { get } = await import("@vercel/blob");
  const found = await get(key, { access: "private", useCache: false, ...(token ? { token } : {}) });
  if (!found?.stream) return null;
  if (found.blob?.size && found.blob.size > maxBytes) return null;
  const chunks: Buffer[] = [];
  let total = 0;
  const reader = (found.stream as unknown as ReadableStream<Uint8Array>).getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      return null;
    }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks);
}

/**
 * Local integration only: a file from the directory store as a download response. Refused
 * whenever this is, or claims to be, the production deployment.
 */
export async function localDownload(key: string, filename: string, contentType: string): Promise<Response | null> {
  if (!process.env.AZMONITOR_OBJECT_STORE_DIR || process.env.VERCEL_ENV === "production"
      || process.env.AZMONITOR_ENVIRONMENT === "production") return null;
  const bytes = await readPrivate(key, 200 * 1024 * 1024);
  if (!bytes) return Response.json({ error: "The catalogue lists this file but storage does not hold it." }, { status: 410 });
  return new Response(new Uint8Array(bytes), { headers: {
    "content-type": contentType,
    "content-disposition": `attachment; filename="${filename.replace(/[^\w.\-]/g, "_")}"`,
    "content-length": String(bytes.length), "cache-control": "private, no-store", "x-content-type-options": "nosniff",
  } });
}
