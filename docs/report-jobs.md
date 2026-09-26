# Report jobs: requesting, publishing and emailing reports

This is how the two user journeys work, how to set them up, and how to run them.

1. **On demand.** A signed-in person opens *Generate*, chooses a report, and gets a job page they can
   follow, leave and come back to. When the job publishes, the files download from the dashboard and,
   if they asked, an email arrives.
2. **Automatically.** Three times a day the system checks the official sources. When a check finds a
   relevant new release or a genuine revision, the affected reports are produced by the same
   pipeline, published once validated, and announced to subscribed recipients.

The architecture is the one already in place — Vercel dashboard, Neon Postgres, private Vercel Blob,
the Python engine on GitHub Actions. What changed is where state lives and what does the scheduling:

```
 Browser ──POST /api/jobs──► Vercel (Next.js)
                               │  validates the request, dedups against active jobs, offers an
                               │  identical published edition, writes job + dispatch row (Neon)
                               │
 Vercel Cron ─ every 15 min ─► /api/cron/tick ── the only scheduler
                               │  09:15 13:15 17:15 Baku: a source-check job, once per slot
                               │  Monday 08:30 Baku: the weekly digest job
                               │  reaps lost workers, redrives dispatches, sends due email, monitors
                               ▼
                         job_dispatches ──workflow_dispatch(job_id)──► GitHub Actions: report-job.yml
                                                                          │
                                python -m azmonitor.jobs run --job-id ... │
                                  claim (fenced lease, heartbeat) ◄───────┘
                                  dataset lease → restore from private Blob
                                  [check sources → classify changes → plan affected reports]
                                  produce with the engine → validate → upload + verify
                                  publish in ONE transaction: publication record, archive entry,
                                    email intents, job outcome
                                  save dataset → refresh dashboard figures → release lease
                               ▲
 Resend ──signed webhooks──► /api/webhooks/resend     email_outbox ──(tick / request)──► Resend
```

## What state lives where

| Table (Neon) | Holds | Written by |
|---|---|---|
| `report_jobs`, `job_events` | every request and scheduled run, its stage, lease, heartbeat, outcome | web (create), worker (everything after) |
| `job_dispatches` | the outbox of runner starts, one open per job | web |
| `publication_records` | the authority for "this edition is published", with its verified file manifest, validation, periods, cutoff, findings, limitations | worker, in the publication transaction |
| `source_changes` | every classified change a check found, and whether a report has answered it | worker |
| `email_outbox`, `email_events` | the delivery ledger and provider events | worker (intents), web (sending, webhooks) |
| `notification_settings`, `recipients`, `subscriptions`, `audit_log` | who gets what, and who changed it | web |

Migrations are numbered and additive (`azmonitor/appstate/schema.py`), applied under a Postgres
advisory lock by whichever side reaches the database first (`python -m azmonitor.appstate migrate`
in the workflow, `ensureSchema()` in the web). None drops or rewrites a table. They were developed
and tested against isolated local databases only; the production database has not been touched.

## The rules that make it safe

- **One job per request.** Postgres computes an equivalence key from the canonical parameters and a
  partial unique index allows one active job per key: two clicks, two tabs or the scheduler asking
  for the same report share a job. Each schedule slot is unique too, so a slot starts once however
  many ticks see it.
- **The browser chooses nothing dangerous.** A request is a report type plus named choices checked
  against the same rules on both sides (`azmonitor/jobs/params.py`, `web/lib/params.ts`, shared test
  cases in `tests/fixtures/request-cases.json`). The repository, workflow and branch are server
  configuration. The runner receives only a job id, validated against its exact shape, and reads the
  request from the database.
- **Leases, heartbeats and fencing.** A worker claims a job with a five-minute lease renewed every
  thirty seconds on its own connection, and every write it makes presents a fence token. A worker
  that stops (crash, preempted runner, lost network) stops renewing; the tick's reaper requeues the
  job with backoff (up to three attempts) and releases the dataset lease the dead attempt held. If
  the old worker wakes up, its next write finds a different fence and stops: it cannot publish.
  Renewal stops after 100 minutes, so a wedged job cannot hold its lease forever.
- **Dispatch accepted is not running.** A dispatch GitHub accepted but no runner claimed within 30
  minutes is sent again (three times at most, then the job fails with the reason). A refused
  dispatch (bad token, missing workflow) fails the job at once with a message that says what to check.
- **Publish once, completely.** Artefacts are validated (PDF opens and has the deck's pages, deck and
  evidence workbook present, grounding, quality, distribution profile), uploaded to a path that
  belongs to this job attempt alone, and each upload is read back. Only then does one transaction
  record the publication, add it to the archive, queue its emails and complete the job.
- **Generation, publication and email are separate states.** A job can publish while its email is
  still queued, fails, or is suppressed; the job page and the ledger show each on its own.

## What is announced, and what is not

A source check classifies what it found (`azmonitor/changes.py`, mapping in `config/triggers.yaml`):

| Classification | Produces reports | Announced |
|---|---|---|
| `new_publication` — released within its window (45/60/10 days by type) | yes | yes |
| `new_observations` — a period beyond the latest held | yes | yes |
| `substantive_revision` — a published value changed | yes (new version) | yes, as a revision, after the settle window |
| `translation` — another language edition | no | no |
| `historical_backfill` — old material seen for the first time | no | no |
| `bytes_only` — file changed, data did not | no | no |

A change stays pending until every report it affects has answered it (published, or found its
inputs unchanged). A monthly waiting for companion tables shows as *Waiting for data* on the check's
page and is planned again by the next check. Changes older than 35 days expire.

Email goes out only for editions caused by new data, a revision, a new publication or the weekly
digest — never for a blocked edition, an unchanged check, a backfill, a translation, a reused
edition or a forced regeneration. A manual "email me" goes to the requester only, and a requester
who is also a subscriber gets one message, not two. Revisions arriving in a burst are coalesced
into one message about the latest version.

Links in email are to the application (report page, authenticated download route), never to storage.
The PDF is attached when it is under the attachment limit (10 MB by default), otherwise linked.

## Environment matrix

| Variable | Where | Production | Preview | Notes |
|---|---|---|---|---|
| `AZMONITOR_DATABASE_URL` | Vercel, GitHub secret | Neon main branch (pooled) | a separate Neon branch | the worker and the web must point at the same database |
| `AZMONITOR_SESSION_SECRET` | Vercel | ✓ | ✓ (different) | signs sessions and unsubscribe links |
| `AZMONITOR_DASHBOARD_PASSPHRASE_HASH` | Vercel | ✓ | ✓ | |
| `AZMONITOR_OWNER_EMAIL` | Vercel | ✓ | ✓ | the only address "email me" uses |
| `CRON_SECRET` | Vercel | ✓ | — | cron only runs on Production |
| `GITHUB_DISPATCH_TOKEN` | Vercel | ✓ | — | fine-grained PAT: this repository, *Actions: read and write*, nothing else |
| `GITHUB_REPOSITORY`, `GITHUB_WORKFLOW_REF` | Vercel | ✓ | — | fixed server-side |
| `RESEND_API_KEY` | Vercel | ✓ | **never** | a sending-only key is enough |
| `AZMONITOR_EMAIL_FROM` | Vercel | ✓ | — | address on the verified domain |
| `RESEND_WEBHOOK_SECRET` | Vercel | ✓ | — | from the Resend webhook |
| `AZMONITOR_APP_URL` | Vercel | ✓ | ✓ | absolute URL for email links |
| `BLOB_STORE_ID`, `VERCEL_OIDC_TOKEN` | injected by Vercel | ✓ (store connected) | per connection | the web reads files with these |
| `AZMONITOR_DATABASE_URL`, `BLOB_STORE_ID`, `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `BLOB_READ_WRITE_TOKEN` | GitHub secrets | ✓ | — | used by `report-job.yml`; see cloud-deployment.md for the Blob credential |

Preview deployments start no runner (dispatch is off unless `AZMONITOR_DISPATCH_MODE` is set) and
send no email whatever keys they hold: the provider refuses to send outside Production, and the
worker records every announcement produced outside Production as suppressed.

## Setup

1. **Database.** Nothing to run by hand: the first worker or web request applies the migrations.
   To apply them deliberately first: `AZMONITOR_DATABASE_URL=... python -m azmonitor.appstate migrate`.
2. **Dispatch.** Create a fine-grained token (GitHub → Settings → Developer settings → Fine-grained
   tokens): resource owner the repository's owner, *Only select repositories* → this one,
   *Repository permissions → Actions: Read and write*. Put it in Vercel as `GITHUB_DISPATCH_TOKEN`
   (Production only). `report-job.yml` must be on the default branch for GitHub to accept a dispatch
   of it; `GITHUB_WORKFLOW_REF` names the branch whose code runs.
3. **Private storage.** Unchanged: the Blob store stays private and connected to the project. The
   worker keeps using the Blob credential path in cloud-deployment.md; the store must be available
   to the environment the minted token is for (see "The environment the token is for" there).
4. **Email.** In Resend: add and verify the sending domain (DNS records), create a *sending access*
   API key, and create a webhook to `https://<production-domain>/api/webhooks/resend` for
   `email.sent`, `email.delivered`, `email.bounced`, `email.complained`, `email.failed`,
   `email.suppressed`, `email.delivery_delayed`. Put the key, the from-address and the webhook's
   signing secret in Vercel (Production only). Until the domain is verified Resend delivers only to
   the Resend account's own address, which is enough for owner-only operation.
5. **Recipients.** Settings → Add recipient: the owner, role *owner*, subscribed to the reports they
   want. Send a test email from the same page.
6. **Activation** (after the controlled checks below succeed): Settings → turn on *Check official
   sources automatically* and *Email subscribers about new editions*. Both are recorded in the audit
   log with who turned them on.

## Operating it

| To | Do |
|---|---|
| retry a failed or blocked job | the job page's *Retry* (a new job with the same request; the old record stays) |
| stop a job | *Cancel*: a queued job stops at once, a running one at its next stage |
| regenerate an unchanged edition | *Force a new version* on Generate (administrators, with a recorded reason; never announced) |
| retry an email | Settings → Delivery ledger → *Retry* on the failed row |
| settle an uncertain email | *It arrived* (records it as sent) or *Send again* (a deliberate new send) |
| pause announcements | Settings → *Pause notifications*; held messages go out on *Resume* |
| add, remove or unsubscribe a recipient | Settings → Recipients; recipients can also use the link in each announcement |
| clear a suppressed address | Settings → Recipients → *Clear suppression* (after fixing the cause) |
| restore after data loss | the dataset in Blob is versioned by pointer; `python -m azmonitor.cloud.publish restore` fetches the current one, and every published edition is re-applied from Postgres by the next worker |
| run a job by hand | `AZMONITOR_DATABASE_URL=... python -m azmonitor.jobs run --job-id job_...` (it claims like any worker) |

## Monitoring

Every tick records what needs a person in the `scheduler_tick` read-model entry, shown on Settings,
and emails owners once per distinct set of problems per day:

- a running job that has not reported progress for 10 minutes;
- jobs failed or blocked in the last 24 hours;
- jobs waiting over 45 minutes for a runner;
- a scheduled check or digest that never started, or ended failed;
- uncertain, failed or long-queued email;
- suppressed recipient addresses.

The job page shows each job's heartbeat age and flags a worker that has gone quiet.

## Tests and what they prove

| Layer | Where | What is real |
|---|---|---|
| Python unit and integration | `pytest` (CI runs it against Postgres 16) | Postgres, SQL functions, the worker with a stand-in engine writing real PPTX/PDF/XLSX |
| Web | `cd web && npm test` (CI with Postgres) | Postgres, route handlers with real Requests, Resend's webhook verification |
| End to end | `python scripts/e2e/run.py` | the engine, LibreOffice, the live CBA website, Postgres, the production build, Chromium |

The end-to-end run substitutes three things and says so in its report: a local directory for private
Blob, the web application's local dispatcher for GitHub Actions, and a capture directory for Resend.
Its test hooks (`AZMONITOR_REFRESH_DATASETS`, `AZMONITOR_FETCH_OVERRIDES`, short leases, the
`test` environment) are refused for any production job.

### What the end-to-end run found

Five defects that no unit test had caught, all fixed and each now pinned by a test:

- **A whole deck failed on one empty chart.** The CBA regional tables carry a single month; an
  edition for any other banking month had no regional rows, python-pptx refused the empty chart and
  the job failed. The slide now says which month the tables hold (`tests/test_render_guards.py`).
- **A saved dataset could miss its newest writes.** SQLite runs in WAL mode and the archive held only
  the main file; the worker's save straight after collecting could leave out what it had just read,
  and a check that produced nothing never saved again. Saves now checkpoint first
  (`tests/test_cloud_persistence.py`).
- **A dead worker blocked every job for half an hour.** It kept the dataset lease; the reaper now
  releases it with the job (`tests/test_appstate.py`).
- **"Email me" waited for the next 15-minute tick.** A watched job page now sends its due email at once.
- **Two workers on one machine could corrupt the stored dataset.** A replaced worker that woke up
  checkpointed its stale SQLite log into the file its successor was archiving; later restores
  failed their integrity check (and correctly refused to run). Each attempt now works in its own
  directory, so the fresh-runner property holds everywhere, not only on GitHub
  (`tests/test_worker.py::test_each_attempt_works_in_its_own_directory_and_leaves_nothing_behind`).

## Remaining steps to go live (user actions)

Everything below needs access this environment does not have — Vercel, Neon and Resend are not
reachable from it, and changing the default branch or production deployment is your decision.
In order:

1. **Blob for the runner — your decision.** The last cloud run stopped here: a token minted outside a
   deployment is always a *development* token, and the store is connected only to Preview and
   Production. There are two ways through, and you have ruled out doing either on my initiative:
   - include *Development* in the store's project connection (Vercel → Storage → the store →
     Projects → Update Project Connection). You asked earlier that this not be done merely to get
     round the mismatch; the argument that it does not widen who can reach the store is in
     cloud-deployment.md ("The environment the token is for"), and the choice is yours; or
   - issue a store-scoped read-write token yourself and save it as the GitHub secret
     `BLOB_READ_WRITE_TOKEN` (a credential change, which you asked me not to make).
   Without one of them no cloud worker can read or write the dataset.
2. **Workflow on the default branch.** Merge a one-file pull request adding
   `.github/workflows/report-job.yml` to `main` (GitHub accepts a dispatch only for a workflow that
   exists on the default branch). Until PR #2 is merged, set `GITHUB_WORKFLOW_REF` to the branch that
   carries the worker code.
3. **Production configuration in Vercel** (Production scope only): `GITHUB_DISPATCH_TOKEN`,
   `GITHUB_REPOSITORY`, `GITHUB_WORKFLOW_REF`, `CRON_SECRET`, `RESEND_API_KEY`,
   `AZMONITOR_EMAIL_FROM`, `RESEND_WEBHOOK_SECRET`, `AZMONITOR_APP_URL`; confirm
   `AZMONITOR_OWNER_EMAIL` is your address. Preview gets none of the dispatch or email variables.
4. **Resend.** Verify the sending domain, create the webhook to `/api/webhooks/resend`.
5. **Deploy to Production** (the Vercel cron runs only on Production deployments).
6. **Controlled checks in production**, in this order, watching each job page:
   Settings → add yourself as the owner recipient → *Send a test email to me* (expect `accepted`,
   then `delivered` once the webhook arrives) → Generate → Monthly Monitor, latest, *Use the latest
   collected data*, *Email me* (expect a published edition, a PDF download, and your email) →
   Generate the same again (expect the identical-edition offer) → Generate with *Check sources for
   updates first* (expect a source check to run and either publish or report no relevant change).
7. **Activate**: Settings → *Check official sources automatically* and *Email subscribers about new
   editions*, with you as the only recipient. The next 09:15, 13:15 or 17:15 Baku slot starts the
   first automatic check; its job appears on the Jobs page.
