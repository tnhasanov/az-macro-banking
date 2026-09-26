/**
 * The GitHub dispatch request, against a stand-in for `fetch`: what is sent, and what each answer
 * GitHub can give turns into. Mocked, not integration — the real call is verified in the cloud
 * run described in docs/report-jobs.md.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { dispatcher, githubDispatcher, WORKFLOW } from "../lib/dispatch.ts";

function fakeFetch(status: number, body: unknown = null, seen: { url?: string; init?: RequestInit }[] = []) {
  return (async (url: string, init: RequestInit) => {
    seen.push({ url, init });
    return new Response(body === null ? null : JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
}

test("the request names a fixed workflow and passes only the job id", async () => {
  const seen: { url?: string; init?: RequestInit }[] = [];
  const d = githubDispatcher("tok", "owner/repo", "main", fakeFetch(204, null, seen));
  const res = await d.dispatch("job_01abcdefghjkmnpqrstvwxyz0");
  assert.equal(res.kind, "accepted");
  assert.equal(seen[0].url, `https://api.github.com/repos/owner/repo/actions/workflows/${WORKFLOW}/dispatches`);
  const body = JSON.parse(String(seen[0].init!.body));
  assert.deepEqual(body, { ref: "main", inputs: { job_id: "job_01abcdefghjkmnpqrstvwxyz0" }, return_run_details: true });
  assert.equal((seen[0].init!.headers as Record<string, string>).authorization, "Bearer tok");
});

test("run details are recorded when GitHub returns them", async () => {
  const d = githubDispatcher("tok", "o/r", "main",
    fakeFetch(200, { workflow_run_id: 99, run_url: "https://api.github.com/x", html_url: "https://github.com/o/r/actions/runs/99" }));
  assert.deepEqual(await d.dispatch("job_x"), { kind: "accepted", runId: 99, runUrl: "https://github.com/o/r/actions/runs/99" });
});

test("a refusal is final and names what to check; an outage is retried", async () => {
  for (const status of [401, 403, 404, 422]) {
    const res = await githubDispatcher("t", "o/r", "main", fakeFetch(status, { message: "secret detail" })).dispatch("job_x");
    assert.equal(res.kind, "rejected");
    assert.doesNotMatch((res as { message: string }).message, /secret detail/);
  }
  for (const status of [429, 500, 502, 503]) {
    assert.equal((await githubDispatcher("t", "o/r", "main", fakeFetch(status)).dispatch("job_x")).kind, "retry");
  }
  const down = githubDispatcher("t", "o/r", "main", (async () => { throw new TypeError("fetch failed"); }) as unknown as typeof fetch);
  assert.equal((await down.dispatch("job_x")).kind, "retry");
});

test("repository and ref come from the server's configuration and are checked", () => {
  const saved = { ...process.env };
  try {
    process.env.AZMONITOR_DISPATCH_MODE = "github";
    process.env.GITHUB_DISPATCH_TOKEN = "t";
    process.env.GITHUB_REPOSITORY = "owner/repo; rm -rf /";
    assert.equal(dispatcher("production").name, "off");
    process.env.GITHUB_REPOSITORY = "owner/repo";
    process.env.GITHUB_WORKFLOW_REF = "main && curl evil";
    assert.equal(dispatcher("production").name, "off");
    process.env.GITHUB_WORKFLOW_REF = "main";
    assert.equal(dispatcher("production").name, "github");
  } finally {
    process.env = saved;
  }
});

test("a preview deployment starts no runner unless told to, and never runs the worker locally in production", () => {
  const saved = { ...process.env };
  try {
    delete process.env.AZMONITOR_DISPATCH_MODE;
    process.env.GITHUB_DISPATCH_TOKEN = "t";
    process.env.GITHUB_REPOSITORY = "o/r";
    assert.equal(dispatcher("preview").name, "off");
    process.env.AZMONITOR_DISPATCH_MODE = "local";
    assert.equal(dispatcher("production").name, "off");
    assert.equal(dispatcher("development").name, "local");
  } finally {
    process.env = saved;
  }
});
