import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function resolve(specifier, context, next) {
  let spec = specifier;
  if (spec.startsWith("@/")) spec = pathToFileURL(path.join(ROOT, spec.slice(2))).href;
  const local = spec.startsWith(".") || spec.startsWith("file:");
  if (local && !path.extname(spec.startsWith("file:") ? fileURLToPath(spec) : spec)) {
    for (const ext of [".ts", ".tsx", "/index.ts"]) {
      try {
        return await next(spec + ext, context);
      } catch {
        // try the next extension
      }
    }
  }
  try {
    return await next(spec, context);
  } catch (error) {
    // Next.js subpath entry points (next/server, next/headers) are CommonJS files without an
    // "exports" map, which the bundler finds and Node's ESM resolver does not.
    if (!local && /^next\/[a-z-]+$/.test(spec)) return next(`${spec}.js`, context);
    throw error;
  }
}
