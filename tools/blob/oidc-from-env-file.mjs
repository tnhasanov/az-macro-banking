#!/usr/bin/env node
/**
 * Turn a file written by `vercel env pull` into the short-lived Blob credential, for CI.
 *
 * Why this exists at all. A private Blob store is opened by one of two credentials: a static
 * read-write token, or a short-lived OIDC token paired with the store id. The read-write token is
 * the one Vercel documents for code outside Vercel — but on a store connected to a project, Vercel
 * stores that token as a *sensitive* environment variable, which is write-only by design: there is
 * no reveal in the dashboard and no API that returns the plaintext. `vercel env pull` therefore
 * hands back `BLOB_READ_WRITE_TOKEN` with an empty value, which is not a fault to debug. It is the
 * feature working.
 *
 * What is obtainable is the other credential. `vercel env pull` writes a fresh `VERCEL_OIDC_TOKEN`
 * — valid about twelve hours — alongside `BLOB_STORE_ID`, and it runs non-interactively with a
 * Vercel access token plus VERCEL_ORG_ID and VERCEL_PROJECT_ID. An OIDC token is issued by Vercel
 * but not confined to it: pulling one is how local development reaches a private store, and a
 * GitHub Actions runner is the same case.
 *
 * So the worker's run credential is a token that expires in hours and is scoped to one store,
 * minted fresh each run. What sits in CI permanently is the access token used to mint it, and that
 * is the part to read carefully — see docs/cloud-deployment.md. It should be project-scoped and
 * given an expiry.
 *
 * This script is deliberately separate from the pull itself so it can be tested without a network
 * or an account: the pull produces a file, and everything that could go wrong afterwards — an empty
 * value, a quoted value, a variable that is not there, a secret reaching the log — happens here.
 *
 * Usage:  node oidc-from-env-file.mjs <file>
 *
 * Writes VERCEL_OIDC_TOKEN and BLOB_STORE_ID to $GITHUB_ENV when that is set, and masks the token
 * in the log first. Prints a JSON summary that names the store and the token's shape, never its
 * value. Exits 1 with a message naming exactly what was missing.
 */
import { readFileSync, appendFileSync } from "node:fs";

/** The variables we take. BLOB_READ_WRITE_TOKEN is deliberately not among them: see above. */
const WANTED = ["VERCEL_OIDC_TOKEN", "BLOB_STORE_ID"];

/**
 * Parse the dotenv the CLI writes.
 *
 * Values arrive quoted (`KEY="value"`), which matters more than it looks: a quoted empty value is
 * `KEY=""`, and reading that as the two-character string `""` would hand an empty credential to the
 * SDK and fail a long way from here with an authentication error rather than here with a reason.
 */
export function parseEnvFile(text) {
  const out = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim().replace(/^export\s+/, "");
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"') && value.length >= 2)
        || (value.startsWith("'") && value.endsWith("'") && value.length >= 2)) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

export function extract(text) {
  const parsed = parseEnvFile(text);
  const found = {};
  const missing = [];
  for (const name of WANTED) {
    const value = (parsed[name] ?? "").trim();
    if (value) found[name] = value;
    else missing.push(name);
  }
  return { found, missing, sawReadWriteKey: "BLOB_READ_WRITE_TOKEN" in parsed };
}

function main(file) {
  if (!file) {
    process.stderr.write("usage: oidc-from-env-file.mjs <file written by `vercel env pull`>\n");
    process.exit(1);
  }
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch (error) {
    process.stderr.write(`could not read ${file}: ${error.message}\n`);
    process.exit(1);
  }

  const { found, missing, sawReadWriteKey } = extract(text);
  if (missing.length) {
    process.stderr.write(
      `the pulled environment has no usable ${missing.join(" and ")}.\n`
      + "Check that the Blob store is connected to this project and that the environment pulled\n"
      + "is the one it is connected to. An empty BLOB_READ_WRITE_TOKEN is expected and not the\n"
      + "problem: Vercel stores it write-only, and this run does not need it.\n");
    process.exit(1);
  }

  // Masked before anything else prints, so a later failure cannot echo the token into the log.
  process.stdout.write(`::add-mask::${found.VERCEL_OIDC_TOKEN}\n`);
  if (process.env.GITHUB_ENV) {
    for (const [name, value] of Object.entries(found)) {
      appendFileSync(process.env.GITHUB_ENV, `${name}=${value}\n`);
    }
  }
  process.stdout.write(JSON.stringify({
    credential: "oidc",
    store_id: found.BLOB_STORE_ID,
    oidc_token_length: found.VERCEL_OIDC_TOKEN.length,
    read_write_token_present_but_empty: sawReadWriteKey,
    exported: Boolean(process.env.GITHUB_ENV),
  }) + "\n");
}

if (import.meta.url === `file://${process.argv[1]}`) main(process.argv[2]);
