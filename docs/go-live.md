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
| 2026-09-28 11:10 | Bootstrap bundle checked on a runner, then check-only against the store | a runner of a private repository (bundle and store token never in this public repository), code `7c13b23` | all 9 parts and the assembled bundle match their checksums; restored with SQLite integrity ok; the token authenticated `read-write`; **store empty**, nothing written |
| 2026-09-28 11:11 | Seed the store from the bundle | same, code `7c13b23` | **seeded**: pointer, state archive (5,851,807 bytes) and source archive (254,156,293 bytes) in the store, sizes read back; the backup then failed re-downloading: *expected 0 bytes, received 5851807* |
| 2026-09-28 11:14 | Read-only probe of what the service returns | same | a 724-byte blob comes back plain with a Content-Length; both archives come back `Content-Encoding: br`, chunked, no Content-Length, weak ETag `W/"…"`. The SDK then reports size 0. Fixed in `9dae188` (size from the metadata API; ETag in strong form) |
| 2026-09-28 11:26 | Verified backup, then restore on a fresh runner | same, code `9dae188` | stored dataset is the bundle's (same source-archive fingerprint); backup `dataset/backups/20260928T112657Z-bootstrap.json` recorded after a restore with SQLite `ok` on both databases; a fresh runner restored the store (383 documents, 37,326 observations, 95 publications, 270 vintages, 12,833 passages, 87 policy decisions, 99 report editions; integrity ok) and the backup again, intact. Each 260 MB restore took about 4 minutes |
| 2026-09-28 11:37 | Preflight (dry run) with this repository's credential | manual run [36416630224](https://github.com/tnhasanov/az-macro-banking/actions/runs/36416630224), commit `9dae188` | token belongs to `BLOB_STORE_ID`'s store; the store holds the seeded dataset (same keys, digests and `saved_at`), no report objects; Neon schema 6, no jobs, email or recipients, no switches (automatic checks and email off); delivery disabled; read model empty until the first production run |

The isolated automatic-path event (`scripts/cloud/isolated_event.py`, below) was tried locally —
local Postgres, a local copy of the production dataset, the live CBA website — on 2026-09-27
04:39–04:44 UTC. CBA's download host (`uploads.cbar.az`) did not complete a TLS handshake that
morning (the main site answered), so the check was requeued as *sources unreachable* rather than
failed, the correction step was skipped with that reason, and the production pointer was unchanged.
The same trial found that a log line written after a job's working directory had been removed
raised instead of being dropped; fixed in `6cf7a5c`.

On 2026-09-27 the fixture version (`--fixture --correction`, commit `f648d67`) passed locally against
a stand-in store seeded from the bootstrap bundle: the source check read the deposits file served for
the URL CBA's page listed (discovery was live), classified it `new_observations`, and published
`monthly:2026-07:v40` (environment `test`); the corrected copy was classified `substantive_revision`
and published `monthly:2026-07:v41` superseding v40; no email was queued; the production pointer was
unchanged. This is a local result; the same task still has to run on the real services.

Nothing else has run against Vercel, Blob, Neon or Resend beyond the rows above. In particular no report has been
requested from a deployed dashboard, no runner has been dispatched by one, and no email has been
sent.

Why not more: from the development environment Vercel, Blob, Neon and Resend are refused by its
network policy, so everything real runs on GitHub's runners, which can reach them; and the steps
below that need your accounts (a store token, a GitHub token, Resend, Vercel production settings)
cannot be done from here.

**Open.** (1) The `windows-seed` job fails since `9dae188`: the stand-in service now compresses
larger downloads the way the real one does, and on Windows the 3.6 MB rehearsal archive arrives
as 0 bytes (the 300 KB case passes; Linux passes at every size, under Node 20 and 22). The Linux
workers are not affected; the Windows reseeding procedure is not currently verified. (2) A 260 MB
restore takes about 4 minutes because the service recompresses the archives on the way out; every
report job pays it.

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

**1. Done — the store token is a GitHub secret** (verified by run 36297069332: `read-write`, the
token's store matches `BLOB_STORE_ID`, authenticated; the store was empty).

**2. Done — the store is seeded from the bootstrap bundle** (2026-09-28, table above), from a
private repository's runner rather than a computer. The procedures below remain for reseeding.
The Windows script's rehearsal currently fails on a larger compressed download (see *Open*).

*Reseeding from a computer:* The development environment
cannot reach Blob, and the bundle does not travel through the repository or any public artifact: it
was handed to you privately — `README-SEED.txt`, `SHA256SUMS`, `PARTS.SHA256SUMS`,
`VERIFICATION.json`, `current.json`, `state-20260920T093543Z-snapshot.tar.gz` and
`raw-20260920T093543Z-snapshot.tar.gz.part-00` … `part-08`.

*On Windows* — no WSL: the seed runs natively. It needs Git, 64-bit Python 3.11 or later (3.12
recommended) and Node 20 or later, and runs in Windows PowerShell 5.1 or PowerShell 7. With the
files above in one folder, from a PowerShell window:

```powershell
git clone https://github.com/tnhasanov/az-macro-banking.git
cd az-macro-banking
git checkout 7c13b23
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\seed-from-bundle.ps1 -Downloads "$HOME\Downloads"
```

`scripts/windows/seed-from-bundle.ps1` checks the three tools' versions; checks each part against
`PARTS.SHA256SUMS` and joins `part-00` to `part-08` in that order; lays the bundle out in a temporary
folder and checks it against `SHA256SUMS`; creates `.venv` and installs this checkout into it
(`pip install -e ".[cloud]"`) and the Blob helper from its lockfile (`npm ci --omit=dev`); asks for
the store's token at a hidden prompt; and runs `publish seed --from-bundle`, which verifies every
digest and SQLite's integrity, refuses a store that already holds a dataset, uploads, reads back
what arrived, and records a backup that it restores once more. Exit 0 is done; 4 means the store
already held a dataset and nothing was uploaded; anything else stopped before or without changing
the store unless its JSON says `"seeded": true`. `-ExecutionPolicy Bypass` applies to that one
process and changes no setting on the machine. The token is typed, not pasted into a command: it
is not echoed, is not in PowerShell's history (which records command lines, not prompt input), is
never written to disk, and exists only in that process's environment until the script removes it.

The `windows-seed` job in `checks.yml` rehearses this on a Windows runner under Windows PowerShell
5.1 on every push, with a stand-in bundle carrying the same file names: it seeds once, refuses a
second seed, refuses a damaged part before installing anything, and runs the post-prompt command
through Python, the Node helper and the SDK against a stand-in Blob service
(`tools/blob/fake-store.mjs`), then restores the result and its backup on a clean directory. What it
cannot rehearse is the real service and the 254 MB multipart upload; the real run is the first test
of those from Windows.

It passed on commit `7c13b23` (runs
[36323604039](https://github.com/tnhasanov/az-macro-banking/actions/runs/36323604039) and
[36323606130](https://github.com/tnhasanov/az-macro-banking/actions/runs/36323606130)) under Windows
PowerShell 5.1.26100 with Python 3.14.7 (found through `py -3`) and Node 20.20.2. Getting there
found and fixed three Windows defects that would otherwise have met you first: the local store
listed keys with backslashes, `oidc-from-env-file.mjs` never recognised that it had been run, and
the archive fingerprint spelled paths with backslashes, which would have made the first Linux run
re-upload the 254 MB source archive.

*On Linux or macOS*, rejoin and check the parts (`cat raw-*.part-0? > raw-….tar.gz`,
`sha256sum -c`), lay the files out as `azmonitor-bootstrap/dataset/…`, and run, from the same
checkout with its Python and Node dependencies installed:

```
read -rs BLOB_READ_WRITE_TOKEN && export BLOB_READ_WRITE_TOKEN   # the store's token, typed locally only
export AZMONITOR_BLOB_AUTH=read-write AZMONITOR_PROFILE=neutral
python -m azmonitor.cloud.publish seed --from-bundle /path/to/azmonitor-bootstrap
```

Either way, the last JSON it prints must show `"seeded": true` and `"backup_restores_intact": true`.
Then I run `preflight` unticked on a runner: a fresh-runner restore of what is now in the store, and
a second verified backup.

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
   The release is served from a fixture — the snapshot's own deposits file, at the URL CBA's page
   lists that day (`--fixture`) — so it does not depend on CBA's download host; a successful live
   download remains a separate, outstanding check. Email on the automatic path is then proven by the
   first genuine release after activation (step 7), which goes to you alone.
6. Preview isolation: on a Preview deployment, Settings shows *preview — never emails subscribers*
   and a test email is recorded `suppressed`.

**7. Activation, after 6 passes.** Settings → *Check official sources automatically* and *Email
subscribers about new editions*, with you as the only recipient. The next slot at 09:15, 13:15 or
17:15 Asia/Baku starts a source-check job, visible on the Jobs page and as a GitHub Actions run.
