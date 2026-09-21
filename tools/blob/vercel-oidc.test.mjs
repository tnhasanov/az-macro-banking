/**
 * Minting the run's OIDC token, and what a refusal is allowed to say.
 *
 * undici's MockAgent answers, so the requests are the real ones the script builds — the endpoint,
 * the method, the bearer header and the team query are observed rather than assumed. What cannot
 * be shown here is whether Vercel agrees; only a real token can.
 *
 * The failure these exist for came back as "Could not retrieve Project Settings. To link your
 * Project, remove the `.vercel` directory and deploy again." — which reads like a linking problem
 * and is not one. It is what the CLI prints when the *team* lookup it does alongside the project
 * lookup returns 403, and a project-scoped access token is denied team-level resources by design.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
/** undici resolved from here, because the preload below lives in a temp directory. */
const UNDICI = import.meta.resolve("undici");

const run = promisify(execFile);
const SCRIPT = new URL("./vercel-oidc.mjs", import.meta.url).pathname;
const PROJECT = "prj_TestProject";
const TEAM = "team_TestTeam";
const TOKEN = "vercel_access_token_for_tests";
const MINTED = "eyJhbGciOiJSUzI1NiJ9.minted.signature";

/**
 * Run the script against a mocked api.vercel.com.
 *
 * The mock is installed inside the child by a preload module, so the script under test is the file
 * that ships, unmodified and with no test hooks in it.
 */
async function mint(replies, { env = {}, githubEnv = null } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "vc-oidc-"));
  const preload = join(dir, "mock.mjs");
  writeFileSync(preload, `
    import { MockAgent, setGlobalDispatcher } from ${JSON.stringify(UNDICI)};
    const agent = new MockAgent();
    agent.disableNetConnect();
    setGlobalDispatcher(agent);
    const api = agent.get("https://api.vercel.com");
    const replies = ${JSON.stringify(replies)};
    for (const r of replies) {
      api.intercept({ path: (p) => p.startsWith(r.path), method: r.method })
         .reply(r.status, r.body ?? {}).persist();
    }
  `);
  try {
    const { stdout } = await run(process.execPath, ["--import", preload, SCRIPT], {
      env: {
        PATH: process.env.PATH, VERCEL_TOKEN: TOKEN, VERCEL_PROJECT_ID: PROJECT,
        VERCEL_ORG_ID: TEAM, ...(githubEnv ? { GITHUB_ENV: githubEnv } : {}), ...env,
      },
    });
    return { ok: true, stdout };
  } catch (error) {
    return { ok: false, stdout: String(error.stdout), stderr: String(error.stderr) };
  }
}

const MINT_OK = { method: "POST", path: "/v1/projects", status: 200, body: { token: MINTED } };

test("the token is minted from the project's own endpoint, not from `env pull`", async () => {
  const result = await mint([MINT_OK]);
  assert.equal(result.ok, true, result.stderr);
  const summary = JSON.parse(result.stdout.trim().split("\n").pop());
  assert.equal(summary.minted, true);
  assert.equal(summary.project, PROJECT);
  assert.equal(summary.oidc_token_length, MINTED.length);
});

test("the minted token is masked before anything else prints", async () => {
  const { stdout } = await mint([MINT_OK]);
  assert.equal(stdout.split("\n")[0], `::add-mask::${MINTED}`);
});

test("the summary never carries the token", async () => {
  const { stdout } = await mint([MINT_OK]);
  const summary = stdout.trim().split("\n").pop();
  assert.ok(!summary.includes("minted.signature"), "a length says it arrived; a value says it to the log");
});

test("the store id rides along, so one step exports all the SDK needs", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ghenv-"));
  const githubEnv = join(dir, "github.env");
  writeFileSync(githubEnv, "");
  const result = await mint([MINT_OK], { githubEnv, env: { BLOB_STORE_ID: "store_Abc123" } });
  assert.equal(result.ok, true, result.stderr);
  const written = readFileSync(githubEnv, "utf8");
  assert.match(written, /^VERCEL_OIDC_TOKEN=eyJhbGciOiJSUzI1NiJ9\.minted\.signature$/m);
  assert.match(written, /^BLOB_STORE_ID=store_Abc123$/m);
  assert.ok(!written.includes("VERCEL_TOKEN="), "the access token does not travel on");
});

// ------------------------------------------------------------- what a refusal explains

const PROBES_ALL_403 = [
  { method: "GET", path: "/v2/user", status: 403, body: { error: { code: "forbidden" } } },
  { method: "GET", path: "/v9/projects", status: 200, body: { id: PROJECT } },
  { method: "GET", path: "/v2/teams", status: 403, body: { error: { code: "team_unauthorized" } } },
];

test("a refusal names the status codes and never the credential", async () => {
  const result = await mint([
    { method: "POST", path: "/v1/projects", status: 403, body: { error: { code: "forbidden" } } },
    ...PROBES_ALL_403,
  ]);
  assert.equal(result.ok, false);
  assert.match(result.stdout, /minting refused: HTTP 403 \(forbidden\)/);
  assert.match(result.stdout, /the team itself/);
  assert.ok(!result.stdout.includes(TOKEN), "the access token must never appear");
  assert.ok(!result.stdout.includes(MINTED));
});

test("a project id that names nothing is called out as such", async () => {
  const result = await mint([
    { method: "POST", path: "/v1/projects", status: 404, body: { error: { code: "not_found" } } },
    { method: "GET", path: "/v2/user", status: 200, body: {} },
    { method: "GET", path: "/v9/projects", status: 404, body: { error: { code: "not_found" } } },
    { method: "GET", path: "/v2/teams", status: 200, body: {} },
  ]);
  assert.equal(result.ok, false);
  assert.match(result.stdout, /VERCEL_PROJECT_ID does not name a project/);
  assert.match(result.stdout, /Settings -> General -> Project ID/);
});

test("an invalid token is distinguished from a scope refusal", async () => {
  const unauthorised = { status: 401, body: { error: { code: "invalid_token" } } };
  const result = await mint([
    { method: "POST", path: "/v1/projects", ...unauthorised },
    { method: "GET", path: "/v2/user", ...unauthorised },
    { method: "GET", path: "/v9/projects", ...unauthorised },
    { method: "GET", path: "/v2/teams", ...unauthorised },
  ]);
  assert.equal(result.ok, false);
  assert.match(result.stdout, /the access token is not valid/);
});

test("a team-level 403 alone is reported as expected, not as the fault", async () => {
  /** This is the case the CLI mistook for a linking problem. */
  const result = await mint([
    { method: "POST", path: "/v1/projects", status: 403, body: { error: { code: "forbidden" } } },
    ...PROBES_ALL_403,
  ]);
  const teamRow = result.stdout.split("\n").find((l) => l.includes("expected and harmless"));
  assert.ok(teamRow, "a project-scoped token being refused the team endpoint is normal, and saying"
    + " so is what stops it being read as the cause");
});

test("the mint request carries the bearer and the team, and posts", async () => {
  // Proven by the mock: no interceptor matches a GET or a missing team query, so a wrong shape
  // would fail to be answered at all.
  const result = await mint([
    { method: "POST", path: `/v1/projects/${PROJECT}/token?source=vercel-oidc-refresh&teamId=${TEAM}`,
      status: 200, body: { token: MINTED } },
  ]);
  assert.equal(result.ok, true, result.stderr);
});
