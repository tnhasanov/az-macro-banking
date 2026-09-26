#!/usr/bin/env node
/**
 * Mint a short-lived Vercel OIDC token for one project, and say precisely why if it cannot.
 *
 * This replaces `vercel env pull`, which cannot work with a project-scoped access token. The CLI's
 * project resolution fetches the *team* alongside the project — `getOrgById(client, link.orgId)`,
 * settled in parallel with the project lookup — and a project-scoped token is denied team-level
 * resources by design. The 403 that comes back surfaces as:
 *
 *     Error: Could not retrieve Project Settings.
 *     To link your Project, remove the `.vercel` directory and deploy again.
 *
 * which reads like a linking problem and is not one. The CLI emits that message only from a 403
 * whose code is `forbidden` or `team_unauthorized`; a wrong project or team id returns 404 and
 * takes a different branch, and an invalid token raises `InvalidToken`. So the message is itself
 * evidence that the token was accepted and the identifiers were found.
 *
 * What is needed is narrower than what `env pull` fetches. `POST /v1/projects/{id}/token` mints an
 * OIDC token for one project and is the call `@vercel/oidc` itself makes to refresh one; the CLI
 * exposes it as `vercel project token`. It is a project resource, so a project-scoped token is the
 * right shape of credential for it.
 *
 * The token is never printed. On failure the script probes a fixed set of endpoints and reports
 * only status codes and Vercel's own error codes, which name what was refused without disclosing
 * anything — so a failed run explains itself instead of needing another one to investigate.
 *
 * Usage:  node vercel-oidc.mjs
 *   env:  VERCEL_TOKEN, VERCEL_PROJECT_ID, VERCEL_ORG_ID (optional but normally required)
 *         BLOB_STORE_ID (passed through, so one step exports everything the SDK needs)
 */
const API = "https://api.vercel.com";

/**
 * The environment a minted token is for, read from its own claims.
 *
 * A Vercel OIDC token names the team, the project *and the environment* it was issued for, and the
 * Blob API checks that environment against the ones the store is connected to. The mint endpoint
 * issues a **development** token — `vercel project token` is documented as "Get a development OIDC
 * token for a project" and takes no environment option — because preview and production tokens are
 * issued to deployments at runtime, not on demand. A runner is therefore always a development
 * caller, whatever branch it is building.
 *
 * Reported here so that a store connected to the wrong environments says so at the point the token
 * is made, rather than two steps later as "OIDC is enabled for this project, but not for the
 * \"development\" environment" — which is accurate and gives no hint that it is a *store
 * connection* that needs changing.
 *
 * The payload is read, not verified: this is a label for a log line, and the API is the thing that
 * decides. Only the environment is taken. The rest of the payload names the owner and the project,
 * and there is no reason to put those in a log.
 */
function environmentOf(jwt) {
  const parts = jwt.split(".");
  if (parts.length < 2) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
    if (typeof payload.environment === "string") return payload.environment;
    // Older tokens carry it only inside `sub`, as `owner:x:project:y:environment:z`.
    const sub = typeof payload.sub === "string" ? payload.sub : "";
    return /(?:^|:)environment:([^:]+)/.exec(sub)?.[1] ?? null;
  } catch {
    return null;
  }
}

/** Status and Vercel's error code. Never a body, which could echo a name or a value. */
async function probe(method, path, token) {
  try {
    const res = await fetch(`${API}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}` },
    });
    let code = null;
    try {
      const body = await res.json();
      code = body?.error?.code ?? null;
    } catch { /* not JSON, which is itself unremarkable */ }
    return { status: res.status, code };
  } catch (error) {
    return { status: 0, code: `network: ${error?.cause?.code ?? error.message}` };
  }
}

/**
 * The four questions a failure could be answering, asked one at a time.
 *
 * Ordered so that each narrows the previous: is the token usable at all, is the project visible
 * with and without a team, is the team visible. A project-scoped token is *expected* to be refused
 * on the user and team endpoints — that is not a fault, and saying so here stops it being read as
 * one.
 */
async function diagnose(token, projectId, orgId) {
  const rows = [
    ["token is usable at all", "GET", "/v2/user",
     "403/401 on every row below too means the token is invalid or expired"],
    ["project, without a team", "GET", `/v9/projects/${projectId}`,
     "404 here means VERCEL_PROJECT_ID does not name a project this token can see"],
    ["project, within the team", "GET", `/v9/projects/${projectId}?teamId=${orgId}`,
     "404 while the row above is 200 means the project is not in VERCEL_ORG_ID's team"],
    ["the team itself", "GET", `/v2/teams/${orgId}`,
     "403 is expected and harmless for a project-scoped token; it is what `vercel env pull` trips over"],
  ];
  const out = [];
  for (const [what, method, path, note] of rows) {
    const { status, code } = await probe(method, path, token);
    out.push({ what, status, code, note });
  }
  return out;
}

function verdict(mint, probes) {
  const byWhat = Object.fromEntries(probes.map((p) => [p.what, p]));
  const project = byWhat["project, within the team"];
  const bare = byWhat["project, without a team"];

  if (probes.every((p) => p.status === 401)) {
    return "the access token is not valid. Create a new one and update the VERCEL_TOKEN secret.";
  }
  if (bare?.status === 404 && project?.status === 404) {
    return "VERCEL_PROJECT_ID does not name a project this token can reach. Check it against the"
      + " project's Settings -> General -> Project ID.";
  }
  if (project?.status === 404 && bare?.status === 200) {
    return "the project exists but is not in the team VERCEL_ORG_ID names. Check the team id.";
  }
  if (mint.status === 403) {
    return "the token reached the project but may not mint a token for it. If it is scoped to a"
      + " single project, confirm that project is this one; a token scoped to a *different*"
      + " project fails exactly this way.";
  }
  if (mint.status === 404) {
    return "the token endpoint was not found for this project, which can mean the project id is"
      + " right but the team query is not.";
  }
  return "unrecognised. The status codes above are the evidence; nothing here is a credential.";
}

async function main() {
  const token = (process.env.VERCEL_TOKEN ?? "").trim();
  const projectId = (process.env.VERCEL_PROJECT_ID ?? "").trim();
  const orgId = (process.env.VERCEL_ORG_ID ?? "").trim();

  const missing = Object.entries({ VERCEL_TOKEN: token, VERCEL_PROJECT_ID: projectId })
    .filter(([, v]) => !v).map(([k]) => k);
  if (missing.length) {
    process.stderr.write(`not set: ${missing.join(", ")}\n`);
    process.exit(1);
  }

  const url = `${API}/v1/projects/${projectId}/token?source=vercel-oidc-refresh`
    + (orgId ? `&teamId=${orgId}` : "");
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
  } catch (error) {
    process.stderr.write(`could not reach ${API}: ${error?.cause?.code ?? error.message}\n`);
    process.exit(1);
  }

  if (res.ok) {
    const body = await res.json().catch(() => null);
    const minted = typeof body?.token === "string" ? body.token.trim() : "";
    if (!minted) {
      process.stderr.write("the token endpoint answered 200 with no token in the body\n");
      process.exit(1);
    }
    // Masked before anything else prints, so a later failure cannot echo it.
    process.stdout.write(`::add-mask::${minted}\n`);
    if (process.env.GITHUB_ENV) {
      const { appendFileSync } = await import("node:fs");
      appendFileSync(process.env.GITHUB_ENV, `VERCEL_OIDC_TOKEN=${minted}\n`);
      const store = (process.env.BLOB_STORE_ID ?? "").trim();
      if (store) appendFileSync(process.env.GITHUB_ENV, `BLOB_STORE_ID=${store}\n`);
    }
    const store = (process.env.BLOB_STORE_ID ?? "").trim();
    if (!store) {
      // Said here rather than left to fail later: an OIDC token names no store by itself, and the
      // SDK will not invent one. The store id is not a secret — it is in the project's environment
      // beside the token Vercel keeps write-only — but it does have to be supplied.
      process.stdout.write(
        "::warning::BLOB_STORE_ID is not set. The token was minted, but OIDC needs the store it"
        + " names; set BLOB_STORE_ID from the project's environment variables.\n");
    }
    const environment = environmentOf(minted);
    if (environment) {
      process.stdout.write(
        `::notice::the minted token is for the "${environment}" environment. The Blob store must`
        + ` be connected to that environment, or the call is refused.\n`);
    }
    process.stdout.write(JSON.stringify({
      minted: true, project: projectId, oidc_token_length: minted.length, store_id: store || null,
      environment,
    }) + "\n");
    return;
  }

  let code = null;
  try { code = (await res.json())?.error?.code ?? null; } catch { /* not JSON */ }
  const mint = { status: res.status, code };
  process.stdout.write(`minting refused: HTTP ${mint.status}${code ? ` (${code})` : ""}\n\n`);

  const probes = await diagnose(token, projectId, orgId);
  process.stdout.write("what was asked                 status  code\n");
  for (const p of probes) {
    process.stdout.write(
      `  ${p.what.padEnd(28)} ${String(p.status).padStart(5)}  ${p.code ?? ""}\n      ${p.note}\n`);
  }
  process.stdout.write(`\n::error::${verdict(mint, probes)}\n`);
  process.exit(1);
}

await main();
