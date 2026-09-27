# Going live: real-cloud verification and activation

This is the runbook for taking the report-jobs system (docs/report-jobs.md) from "proven locally" to
"operating on the real services", and the record of what has been verified against them so far.
The system is **not operational yet**: the real-cloud journey has not run end to end.

## What has been verified against the real services

| When (UTC) | What | Where | Result |
|---|---|---|---|
| 2026-09-27 04:26 | Application database reachable from a GitHub runner; schema version | manual run [36294299961](https://github.com/tnhasanov/az-macro-banking/actions/runs/36294299961), commit `be0195a`, dry run | reachable; schema version 0, migrations 1–6 pending |
| 2026-09-27 04:26 | Blob credential chosen explicitly and proved | same run | mode `oidc` (no `BLOB_READ_WRITE_TOKEN` secret); refused: *OIDC is enabled for this project, but not for the "development" environment* |
| 2026-09-27 04:28 | Application-state migrations on the real Neon database | manual run [36294374181](https://github.com/tnhasanov/az-macro-banking/actions/runs/36294374181), commit `be0195a` | applied 1–6, now version 6; no jobs, publications, email or recipients; no settings rows (automatic checks and email are off by default) |
| 2026-09-27 04:29 | Building the dataset from the official websites on a runner | manual run [36294459892](https://github.com/tnhasanov/az-macro-banking/actions/runs/36294459892), commit `cb2180b`, dry run | cancelled at the 60-minute limit: `www.cbar.az` and the statistics committee answered, every file on `uploads.cbar.az` timed out (90 s, four times each). Led to the per-host cut-off (`fd55dde`) |
| 2026-09-27 05:30 | Preflight with the store token | manual run [36297069332](https://github.com/tnhasanov/az-macro-banking/actions/runs/36297069332), commit `fd55dde`, dry run | Neon schema 6, nothing pending, no jobs, recipients or switches; Blob `read-write`, token belongs to `BLOB_STORE_ID`'s store, authenticated; **store empty** (no dataset, no report objects) |
| 2026-09-27 05:33 | Seed the empty store from the official sources | manual run [36297517710](https://github.com/tnhasanov/az-macro-banking/actions/runs/36297517710), commit `48177b3` | build finished in 20 min thanks to the cut-off: statistics committee and `www.cbar.az` publications collected (237 documents, 7,306 observations, 54 publications; SQLite integrity ok), every CBA table on `uploads.cbar.az` failed to download (21 datasets); **seeding refused** ("the build reported source errors; not seeding a partial dataset"); store still empty |

The isolated automatic-path event (`scripts/cloud/isolated_event.py`, below) was tried locally —
local Postgres, a local copy of the production dataset, the live CBA website — on 2026-09-27
04:39–04:44 UTC. CBA's download host (`uploads.cbar.az`) did not complete a TLS handshake that
morning (the main site answered), so the check was requeued as *sources unreachable* rather than
failed, the correction step was skipped with that reason, and the production pointer was unchanged.
The same trial found that a log line written after a job's working directory had been removed
raised instead of being dropped; fixed in `6cf7a5c`.

Nothing else has run against Vercel, Blob, Neon or Resend. In particular no report has been
requested from a deployed dashboard, no runner has been dispatched by one, and no email has been
sent.

Why not more: from the development environment Vercel, Blob, Neon and Resend are refused by its
network policy, so everything real runs on GitHub's runners, which can reach them; and the steps
below that need your accounts (a store token, a GitHub token, Resend, Vercel production settings)
cannot be done from here.

## Findings from inspecting the deployment

- **Production has no working dashboard.** Vercel builds Production from `main`, which carries only
  `.github/workflows/manual-run.yml` and a README; both Production deployments from it failed
  (2026-09-20, 2026-09-21). Every Preview of `claude/vercel-deployment` builds (latest: `cb2180b`).
- **Plan and schedule.** The Vercel plan is Pro (recorded in docs/costs.md; this environment cannot
  read the billing API). Pro runs cron jobs at per-minute precision, so the single cron in
  `web/vercel.json`, `*/15 * * * *` → `/api/cron/tick`, is supported. On Hobby any expression that
  fires more than once a day fails at deploy time; the smallest compatible change there would be to
  keep the same tick endpoint and move its trigger to one GitHub Actions `schedule` calling it with
  `CRON_SECRET` — still one scheduler. Not needed on Pro.
- **Repository secrets present:** `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`,
  `BLOB_STORE_ID`, `AZMONITOR_DATABASE_URL`, `AZMONITOR_ARCHIVE_BASE_URL`, `AZMONITOR_OWNER_EMAIL`.
  **Missing:** `BLOB_READ_WRITE_TOKEN`.
- **The repository is public**, so workflow logs are public. Every check writes counts, switches and
  modes, never an address, a store id or a credential.

## Which code runs where

- The dashboard is whatever Vercel deploys. Each job it creates records the commit it was built
  from (`VERCEL_GIT_COMMIT_SHA`, a Vercel system variable), server-side.
- It dispatches `report-job.yml` on the branch it was deployed from (`VERCEL_GIT_COMMIT_REF`,
  overridable with `GITHUB_WORKFLOW_REF`). A branch moves, so the runner does not simply run its
  head: it reads the job's recorded commit, and when that commit is in the branch's history it
  checks out **that exact commit** and installs it. A commit that is not on the branch is never
  fetched from anywhere else.
- The worker records the commit it actually runs (`git rev-parse HEAD`, not the dispatched branch's
  `GITHUB_SHA`) and refuses a job whose recorded dashboard commit differs from it, before it restores
  anything. The job page shows both (*Code: dashboard abc1234 · runner abc1234*).
- `report-job.yml` must also exist on `main` for GitHub to accept a dispatch at all: that is PR #5,
  one file, no trigger but `workflow_dispatch`. Its copy on `main` is only the registration; each run
  uses the copy on the dispatched branch.

## Private storage: the store token

The runner needs a credential for the private Blob store. The existing OIDC path mints a
*development* token, and the store is connected to Preview and Production only, so it is refused
(verified above). The narrowest fix that does not change the store's connections is the store's own
read-write token:

- scope: that one store, read and write, no other Vercel resource; no environment attached
- lifetime: long-lived until rotated in the store's settings
- where it lives: a GitHub Actions secret, never in the browser, never in a log

How the worker uses it (verified with the real SDK, `tools/blob/conformance.test.mjs`):
`@vercel/blob` 2.8.0 on its own prefers any valid OIDC token over `BLOB_READ_WRITE_TOKEN` in the
environment. The worker therefore passes the store token as an explicit option (which the SDK ranks
first), hands no other credential to the helper, sets `AZMONITOR_BLOB_AUTH=read-write` so any other
outcome is a refusal, and refuses a token whose store (read with the SDK's own rule) differs from
`BLOB_STORE_ID`. `publish check` reports the mode and whether the store matched.

## Dataset safety before testing

- **Backup:** `python -m azmonitor.cloud.publish backup --label <word>` restores the current dataset
  into an empty directory, checks every digest and SQLite's `integrity_check` on each database, then
  records an immutable backup naming those archives (archives are never deleted by the code, and
  retention treats backed-up ones as in use). `--verify <key>` restores a backup and checks it again.
  The manual run's `preflight` task (not a dry run) does both on a fresh runner.
- **A worker that loses ownership cannot publish or overwrite shared state, even if it resumes
  later:** publication commits check the job's fence inside the publishing transaction; the dataset
  pointer moves only by compare-and-set against the version the worker restored from (Blob
  `ifMatch`), so a worker paused between its last check and the write is refused by the store
  itself; each read-model step pins the dataset lease inside its own transaction; report files go
  to per-attempt paths and never overwrite. Per-attempt working directories keep a replaced worker
  from damaging even the local copy its successor uses. Tests: `tests/test_cloud_persistence.py`,
  `tests/test_worker.py`, `tests/test_lease_fencing.py`.

## "Latest available"

The monthly edition month is the earliest of the latest periods held for its banking anchors
(`config/reports.yaml`: `cba_loans_by_institution`, `cba_deposits`), read from the data on every
request. CBA's 25 September deposits file carried July and August, while loans by institution
ended in July, so the common month was July. The local run's seed had withdrawn July deposits, which
is why its first "latest" (J1) was June and the check that downloaded the live file made it July
(J5). Production is not pinned: when CBA publishes August loans the same request produces August.
`tests/test_latest_period.py` drives the real engine through exactly these cases and fails if a
literal reporting period appears in code or configuration.

## Seeding

**CBA downloads failed from both tested environments on 2026-09-27.** `uploads.cbar.az`, which serves
every CBA statistical table, did not complete TLS handshakes (and answered HTTP 503 over plain HTTP)
from GitHub's runners and from the development environment, while `www.cbar.az`, at the same
address, answered. There is no independent evidence of a general outage; what is known is that
downloads failed from these two environments. A full backfill is not retried automatically.

**Instead, the store is seeded from the validated local snapshot** — a bootstrap bundle in the store's
own layout (`dataset/current.json` and the two archives it names), verified before it left the
development machine and again by `publish seed --from-bundle` before anything is uploaded:

| | |
|---|---|
| last successful source check | 2026-09-20 09:35 UTC (not freshly collected) |
| CBA banking tables and deposits | to end-July 2026 — the monthly edition month is 2026-07 |
| SSC prices and macro headline | to end-August 2026 |
| SSC quarterly GDP | to Q1 2026 |
| documents | 383, every one matching its recorded fingerprint (202 by SHA-256, 181 HTML pages by article fingerprint) |
| observations, publications | 37,326; 95 |
| SQLite | `integrity_check` ok on both databases; quality checks: 0 critical, 5 warnings outside the displayed window |
| excluded | credentials, recipients (the delivery ledger is empty), logs, analytics, local snapshots |

An edition built from it states its information cutoff as the date of the last successful
collection (2026-09-20), not the day it was generated, and the Generate page shows the same date as
*Last collected from the sources*. A successful live source refresh remains a separate, outstanding
check.

The dataset is not in git (about 34 MB of SQLite and 280 MB of downloaded source documents). The
only built copy is on the development machine, which cannot reach the store, so if the store is
empty a runner builds one from the official websites: manual run, task `seed-from-sources`. A dry
run builds and reports what it would upload; unticked, it seeds the store **only if the store holds
no dataset** and the build reported no source errors, then pins a verified backup.

## The steps, in order

**1. You — create the store token as a GitHub secret.** Do not paste it anywhere else, including
this chat.
1. Vercel → **Storage** → the Blob store this project uses → the **.env.local** tab (or
   *Quickstart*) → reveal `BLOB_READ_WRITE_TOKEN` and copy its value.
2. GitHub → this repository → **Settings → Secrets and variables → Actions → New repository
   secret** → Name `BLOB_READ_WRITE_TOKEN` → paste → **Add secret**.

If the store page shows no read-write token, say so and we will use the other route (adding
Development to the store's project connection, which is your decision).

**2. Me — prove it, then back up or seed.** Manual run, task `preflight`, dry run: the log must show
`"credential": "read-write token"`, `"token_store_matches_blob_store_id": true` and what the store
holds. Then, if it holds a dataset, `preflight` unticked (verified backup); if it is empty,
`seed-from-sources` unticked (build, seed, verified backup).

**3. You — merge PR #5** (registers `report-job.yml` on `main`; starts nothing).

**4. You — Vercel Production.** Production must build the dashboard, which `main` does not carry:
Settings → **Git → Production Branch** → `claude/vercel-deployment`. Then, under Settings →
Environment Variables, **Production only**:

| Variable | Value |
|---|---|
| `GITHUB_DISPATCH_TOKEN` | fine-grained token: GitHub → Settings → Developer settings → Fine-grained tokens → resource owner `tnhasanov`, *Only select repositories* → `az-macro-banking`, Repository permissions → **Actions: Read and write**, nothing else |
| `GITHUB_REPOSITORY` | `tnhasanov/az-macro-banking` |
| `GITHUB_WORKFLOW_REF` | leave unset (the runner follows the Production branch) |
| `CRON_SECRET` | a new random value of 32+ characters; Vercel sends it to the cron itself |
| `AZMONITOR_DATABASE_URL` | the same Neon database as the GitHub secret |
| `AZMONITOR_SESSION_SECRET`, `AZMONITOR_DASHBOARD_PASSPHRASE_HASH` | as for Preview, with a different session secret |
| `AZMONITOR_OWNER_EMAIL` | your address |
| `AZMONITOR_APP_URL` | the Production URL |
| `RESEND_API_KEY`, `AZMONITOR_EMAIL_FROM`, `RESEND_WEBHOOK_SECRET` | from step 5 |

Keep *Automatically expose System Environment Variables* on (it provides `VERCEL_GIT_COMMIT_SHA` and
`VERCEL_GIT_COMMIT_REF`), and keep the Blob store connected to Production. Preview gets none of the
dispatch or email variables. Redeploy Production.

**5. You — Resend.** Add and verify the sending domain (DNS records), create a *sending access* API
key, and add a webhook to `https://<production-domain>/api/webhooks/resend` for `email.sent`,
`email.delivered`, `email.bounced`, `email.complained`, `email.failed`, `email.suppressed`,
`email.delivery_delayed`; copy its signing secret into `RESEND_WEBHOOK_SECRET`.

**6. Controlled checks on Production** (you in the browser; I follow each on GitHub, in the
database preflight and — with your connected Gmail — in your inbox):

1. Settings → add yourself as the *owner* recipient → *Send a test email to me*: ledger `accepted`,
   then `delivered` when the webhook arrives; the message in your inbox.
2. Generate → Monthly Monitor, latest, *Use the latest collected data*, *Email me*: a job page, a
   GitHub Actions run linked from it, *Code* showing the same commit twice. Close the browser, reopen
   the job from *Jobs*. Download the PDF, PPTX and workbook. The email arrives with the PDF.
3. The same request again: the identical-edition offer; no new edition and no email.
4. Preflight with a new backup (fresh-runner restore of the dataset the run just saved).
5. Automatic path, isolated (me): manual run, task `isolated-event`, unticked. The scheduler's own
   source-check job on a runner, against the real database and store, with a copy of the
   production dataset under `isolated/run-<id>/` in which the newest deposits month is withdrawn:
   the live CBA file must publish a new edition, and a corrected copy of it a revision. Its jobs
   and publications are environment `test`, never shown by the production dashboard; its
   announcements are recorded as suppressed; the production pointer is compared before and after.
   Needs the CBA download host to be answering. Email on the automatic path is then proven by the
   first genuine release after activation (step 7), which goes to you alone.
6. Preview isolation: on a Preview deployment, Settings shows *preview — never emails subscribers*
   and a test email is recorded `suppressed`.

**7. Activation, after 6 passes.** Settings → *Check official sources automatically* and *Email
subscribers about new editions*, with you as the only recipient. The next slot at 09:15, 13:15 or
17:15 Asia/Baku starts a source-check job, visible on the Jobs page and as a GitHub Actions run.
