# Deploying the monitor to Vercel, Neon and GitHub Actions

Three services, set up in this order, then a staged sequence that keeps code integration, data
seeding, dry runs, real processing, scheduling and email delivery separate from one another.

Nothing before step 6 sends anything to anybody, and nothing here creates a paid resource: every
service is used inside its free allowance at this workload. `costs.md` shows the arithmetic.

For the reasoning behind this shape, read [`vercel-architecture.md`](vercel-architecture.md).

```
   Vercel Cron ─────► /api/cron/<task>  (watchdog: dispatches only an occurrence that was missed)
                              │
   GitHub Actions cron ───────┴──► scheduled.yml
                                        │  takes the database lease, carries its fence token
                                        │  restores the dataset from private Blob
                                        │  refresh → validate → readiness → report → deliver
                                        │  saves the dataset and any new editions + catalogue
                                        │  publishes the read model — only if the task succeeded
                                        ▼
                    Neon Postgres  ◄──── read model (27 MB) ────►  Vercel dashboard
                    Private Blob   ◄──── dataset + archive + catalogue ─┘  (streamed downloads)
```

---

## 1. Neon Postgres

1. Create a project. Any region; `eu-central-1` keeps it near both the runner and the dashboard.
2. Copy the **pooled** connection string (it contains `-pooler`). The dashboard opens many short
   connections and the pooled endpoint is what serverless wants.
3. Nothing else. The worker creates its own schema on first run and migrates it in place afterwards.

The read model is 27 MB against a 0.5 GB free-tier allowance, and it is rebuilt from the dataset
rather than accumulated, so it does not grow without bound.

## 2. Vercel Blob — private

**Everything is written with `access: 'private'`.** A private blob has no publicly readable URL at
all: reads carry the credential in an Authorization header. That is why the dashboard streams
report files through its own authenticated route rather than redirecting to storage — a URL copied
out of the network tab is worth nothing to anyone who is not signed in.

### Which credential, and where

A private store takes either of two, and which one is available is decided by *where the code runs*.

| | credential | lifetime | scope |
|---|---|---|---|
| OIDC | `VERCEL_OIDC_TOKEN` + `BLOB_STORE_ID` | ~12 hours, reissued on demand | one store |
| Read-write token | `BLOB_READ_WRITE_TOKEN` | until revoked | one store |

Connecting a store to a Vercel project gives that project OIDC: Vercel injects the token and
`BLOB_STORE_ID`, and the SDK pairs them without being asked. That is what the **dashboard** uses,
and it needs no configuration.

### Why the read-write token cannot be put in a GitHub secret

Opting the read-write token into a project connection adds it to that project's environment as a
**sensitive** variable. A sensitive variable is write-only by design: there is no reveal in the
dashboard, the copy button is disabled, and no API returns the plaintext. `vercel env pull`
accordingly hands back `BLOB_READ_WRITE_TOKEN` with an **empty value**.

That is the feature working, not a fault to debug — and it is load-bearing for everything below.
The token exists and the deployment can use it; nobody, including the account owner, can read it
back to paste somewhere else. There is no supported way to recover it, and creating a second store
to get a readable one is not an option because the dataset lives in this store.

### What the worker uses instead

An OIDC token is *issued* by Vercel but not *confined* to it. `vercel env pull` writes a fresh one,
which is exactly how local development reaches a private store — and a GitHub Actions runner is the
same case. The pull runs non-interactively with a Vercel access token plus `VERCEL_ORG_ID` and
`VERCEL_PROJECT_ID`.

So each run mints its own credential:

```
vercel env pull  ──>  VERCEL_OIDC_TOKEN (~12h) + BLOB_STORE_ID  ──>  the SDK, unchanged
```

`tools/blob/oidc-from-env-file.mjs` takes only those two out of the pulled file, masks the token
before anything is printed, and deletes the file. `BLOB_READ_WRITE_TOKEN` is never taken from it,
empty or not.

**Read this part carefully, because it is the real cost.** What sits in CI permanently is the
access token used to mint the short-lived one, and it is a *Vercel access token*, not a Blob
credential:

| | a Blob read-write token | a project-scoped Vercel access token |
|---|---|---|
| reaches | one Blob store | everything belonging to one project |
| can it deploy code? | no | **yes** |
| can it read env vars? | no | non-sensitive ones, **yes** |
| can it read sensitive env vars? | no | no — write-only for everyone |
| other projects, team or user resources | no | no — denied |
| expiry | none | **set one**, from 1 day to 1 year |
| revocable | by rotating the store connection | yes, from the tokens page |

A project-scoped token is *not* account-wide — Vercel denies its requests to any other project, to
team-level resources and to user-level resources. But it is broader than a store credential, and
the honest comparison is that it can reach the store **and** the deployment, because code it
deploys would itself hold OIDC. Choosing **All Projects** instead of this one project creates a
team-scoped token; do not.

This is a deliberate trade, made because the narrower credential is unobtainable, not because it is
better. It is mitigated by scope (one project), by expiry (set one) and by the run credential
actually used for storage being a token that dies in hours.

If a readable store-scoped token ever becomes available, nothing needs rewriting: set
`BLOB_READ_WRITE_TOKEN` as a repository secret and the minting step skips itself.

### What was considered and rejected

| Option | Why not |
|---|---|
| Read the existing read-write token from the dashboard, `env pull`, or the API | Impossible by design — sensitive variables are write-only. |
| Have the dashboard expose the token to its signed-in owner | An endpoint whose purpose is to disclose a secret, defeating the storage that is designed never to reveal it. One weak session and the store is gone. |
| Signed delegation tokens (`issueSignedToken` / `presignUrl`) | Shaped for browser uploads, not an external worker: the server SDK's `put`/`get`/`head`/`list`/`del` take only `token`/`oidcToken`/`storeId`, `presignedUrlPayload` appears only in the client flow, and `list` is not a delegable operation at all — so `publish verify` and pruning could not work. It would also mean hand-driving presigned multipart for the 254 MB dataset, which is the protocol guesswork adopting the SDK was meant to avoid. |
| A team-scoped or account-scoped Vercel token | Strictly broader than the project-scoped one for no benefit. |

### Proving it before anything writes

```bash
python -m azmonitor.cloud.publish check
```

One listing, no writes. It reports which credential resolved and whether it opens the store, and
exits non-zero when it does not. Both workflows run it after minting and before taking the lease, so
a run cannot spend an hour refreshing and rendering only to fail at the point of saving.

In a Codespace, where `vercel link` and `vercel env pull` have already run, the same check works
against the real store with no secrets at all:

```bash
set -a; . .env.local; set +a      # BLOB_READ_WRITE_TOKEN="" is ignored; empty counts as absent
python -m azmonitor.cloud.publish check
```

That is the cheapest way to confirm the credential before any of this reaches Actions.

## 3. The engine, on GitHub Actions

Set these as **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | What it is | Needed for |
|---|---|---|
| `AZMONITOR_DATABASE_URL` | Neon pooled connection string | the read model and the run lease |
| `VERCEL_TOKEN` | **project-scoped** Vercel access token, with an expiry | minting the Blob credential each run |
| `VERCEL_ORG_ID` | team id from the project's settings | so the pull knows which project |
| `VERCEL_PROJECT_ID` | project id from the project's settings | so the pull knows which project |
| `BLOB_READ_WRITE_TOKEN` | **leave unset** | unreadable by design; see above. Set it only if a readable one ever exists, and the minting step will skip itself |
| `AZMONITOR_ARCHIVE_BASE_URL` | public base of the dashboard | links in emails |
| `AZMONITOR_OWNER_EMAIL` | the one authorised test recipient | delivery routing |
| `AZMONITOR_GRAPH_TENANT_ID` | Entra tenant | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_ID` | app registration | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_SECRET` | client secret | Microsoft Graph email |
| `AZMONITOR_GRAPH_SENDER` | mailbox to send as | Microsoft Graph email |

`VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` are identifiers rather than secrets, but they are kept as
secrets so a fork or a log cannot casually name the project the credential belongs to.

The four Graph secrets can stay unset until you authorise a live email test. Delivery is disabled in
`config/delivery.yaml` (`enabled: false`) regardless, so an unset credential is not what is stopping
mail from going out — the configuration is.

The workflow sets `AZMONITOR_PROFILE: neutral`. Leave it. It selects the distribution profile that
renders without the bank's logo, brand colours or name, and `publish save` **refuses to upload at
all** under a profile that does not permit external distribution. Removing that line does not
publish branded reports; it stops every run.

### The GitHub rule that decides the order of everything else

**A workflow is registered from the default branch.** Until `scheduled.yml` is on `main` it has no
workflow id, does not appear in the Actions list, and cannot be dispatched — not from the UI, not
from the API. Confirmed against this repository: the Actions API lists `checks.yml` and nothing
else, while `scheduled.yml` sits on `claude/vercel-deployment` and is invisible.

That rules out the obvious sequence. You cannot "run the scheduled workflow by hand before merging",
because there is nothing to run. And merging it to get the button would also arm its `schedule:`
triggers, because those fire from the default branch and nowhere else — so the merge that gives you
a test button is the same merge that starts unattended operation.

Three ways out were considered:

| Option | Why not |
|---|---|
| Merge `scheduled.yml` with the cron lines commented out | Works, but the file that is reviewed is not the file that runs, and uncommenting is a second merge with no review of its own. |
| Run the engine locally against the cloud services | Tests the engine, not the runner: no Actions environment, no repository secrets, none of the packaging that a real run depends on. |
| **A separate, manual-only workflow on the default branch** | **Chosen.** |

`.github/workflows/manual-run.yml` has `workflow_dispatch` and nothing else, so its presence on the
default branch cannot start anything. It checks out whichever branch you name — which it must,
since `main` holds no engine — and runs that branch's code with repository secrets. It defaults to
a dry run, shares the scheduled workflow's concurrency group so the two can never overlap, and
refuses to start at all if delivery has been switched on.

It is on the default branch now, with the repository owner's explicit approval, as a single file
that touches nothing else. Dispatching it offers a branch to run from: choose
`claude/vercel-deployment`, so the run uses that branch's copy of the workflow as well as its
engine.

Delete it once `scheduled.yml` is merged and running. It is scaffolding, not architecture.

**Also worth knowing:** scheduled workflows are disabled after 60 days without repository activity.
That is one of the two failure modes the Vercel watchdog exists to catch; the other is GitHub's cron
being best-effort under load.

## 4. The dashboard, on Vercel

The project root is `web/`. Framework preset Next.js; no build-command override is needed.

Generate the two secrets first:

```bash
# Session signing key
openssl rand -base64 48

# Passphrase digest. Choose a long passphrase; it is never stored, only this digest is.
cd web && node -e '
const { webcrypto } = require("crypto"); globalThis.crypto = webcrypto;
(async () => {
  const pass = process.argv[1];
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(pass), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations: 600000, hash: "SHA-256" }, key, 256);
  const b64 = b => Buffer.from(b).toString("base64");
  console.log(`pbkdf2$600000$${b64(salt)}$${b64(new Uint8Array(bits))}`);
})();' "your passphrase here"
```

| Variable | Value | Notes |
|---|---|---|
| `AZMONITOR_DATABASE_URL` | Neon pooled string | read-only use; the dashboard never writes |
| `AZMONITOR_SESSION_SECRET` | `openssl rand -base64 48` | at least 32 characters, or the app refuses to serve |
| `AZMONITOR_DASHBOARD_PASSPHRASE_HASH` | the `pbkdf2$…` digest above | not the passphrase |
| `BLOB_READ_WRITE_TOKEN` | **omit it** on Vercel | the store is connected to the project, so the deployment authenticates with OIDC; set it only if the store is not connected |
| `CRON_SECRET` | `openssl rand -hex 32` | Vercel sends this to the cron route |
| `AZMONITOR_OWNER_EMAIL` | your address | shown as the signed-in identity |
| `AZMONITOR_CRON_ENABLED` | **unset** | leave unset until you authorise scheduled dispatch |
| `GITHUB_DISPATCH_TOKEN` | fine-grained PAT, *Actions: write* on this repo only | only once dispatch is enabled |
| `GITHUB_REPOSITORY` | `owner/repo` | |
| `GITHUB_WORKFLOW_REF` | branch to dispatch, default `main` | |

`BLOB_STORE_ID` is not in the table because you do not set it: connecting the store to the project
is what puts it there, alongside the OIDC token the dashboard authenticates with. If it is absent
*and* no read-write token is set, the download route answers 503 rather than serving a file it
cannot authenticate for.

**On a preview deployment, set none of the last four.** With `AZMONITOR_CRON_ENABLED` unset the
watchdog reports what it would have done and dispatches nothing.

---

## The staged sequence

Seven stages. Each adds exactly one capability, each has something you can look at to know it
worked, and nothing skips ahead. **Stages A to E send nothing to anybody and create no paid
resource.**

Throughout: ☐ = needs you, ☑ = already done and in the branch.

### Stage A — infrastructure

☑ Neon project created, **pooled** connection string in hand (it contains `-pooler`).
☑ Private Vercel Blob store created and connected to the project.
☑ `.github/workflows/manual-run.yml` committed to `main`, so it can be dispatched at all.

☐ **Create the Vercel access token the worker mints its credential with.**

In the Vercel dashboard:

1. Avatar (top right) → **Account Settings** → **Tokens** → **Create**.
2. **Scope:** select the single project `az-macro-banking`. Do **not** select *All Projects* —
   that makes a team-scoped token, which is broader for no benefit.
3. **Expiration:** pick the shortest span you are willing to renew. 90 days is a reasonable start;
   put the renewal in a calendar.
4. Copy the token **now** — this is the one screen that shows it.

Then, for the two identifiers the pull needs:

5. Project → **Settings** → **General**, and scroll to the bottom for **Project ID**.
6. **Team ID** is on the team's own Settings → General page. (It is also `VERCEL_ORG_ID` in
   `.vercel/project.json` if you have run `vercel link` in a Codespace.)

☐ **Add the repository secrets** — GitHub → Settings → Secrets and variables → Actions → New
repository secret, one each:

| Name | Value |
|---|---|
| `VERCEL_TOKEN` | the token from step 4 |
| `VERCEL_ORG_ID` | the team id |
| `VERCEL_PROJECT_ID` | the project id |
| `AZMONITOR_DATABASE_URL` | the Neon pooled string |
| `AZMONITOR_OWNER_EMAIL` | your address |

Leave `BLOB_READ_WRITE_TOKEN` unset. It cannot be read, and the run does not need it.

**Verified when:** the Actions tab lists a *manual run* workflow with a **Run workflow** button, and
a dry run reaches "credential minted from the … environment" followed by a green credential check.

☑ Everything that has to exist in the repository for this to work is on the branch already.

### Stage B — seed the data

**This stage is optional, and probably skip it.** Seeding uploads an *existing* validated dataset
so the first run does not have to collect one. That dataset is not in version control (`data/` is
ignored, and it is 379 MB), so it exists only where it was built. A fresh runner handles an empty
store as a first run and collects from the public sources in about eleven minutes, which is the
cheaper path unless you already have the files somewhere durable.

The report archive is refused by the artefact guard in any case: all 95 decks, 36 workbooks and 95
PDFs in it were rendered under the branded profile.

If you do have the dataset on a machine with a credential (a Codespace after `vercel env pull`
counts):

```bash
export AZMONITOR_PROFILE=neutral
python -m azmonitor.cloud.publish check             # proves the credential; writes nothing
python -m azmonitor.cloud.publish seed --check      # verifies; uploads nothing
python -m azmonitor.cloud.publish seed              # the dataset only
```

**Verified when:** `seed` reports `"seeded": true` and both halves show
`bytes_in_store == bytes_expected`. Then, on any machine:

```bash
AZMONITOR_DATA_DIR=/tmp/fresh python -m azmonitor.cloud.publish restore
python -m azmonitor.cloud.publish readmodel
python -m azmonitor.cloud.publish verify
```

**Verified when:** `verify` reports `"ok": true`, no missing files and no orphans.

**About the report archive.** `seed --with-reports` will refuse the archive as it stands, and it is
right to: every deck, workbook and PDF in `outputs/` was rendered under the branded profile and
carries the bank's logo or palette. Three options, and this one is yours:

1. **Seed no reports.** The dashboard starts with an empty archive and fills as the engine runs.
   Nothing branded leaves your machine. Simplest, and the default.
2. **Re-render neutrally first**, then seed. Produces new versions of the current editions with no
   bank identity, at the cost of new version numbers.
3. **Override** with `AZMONITOR_ALLOW_RESTRICTED_UPLOAD=i-own-this-content`, if you have the
   standing to publish the bank's branded material to personal infrastructure. I have not assumed
   you do.

### Stage C — one controlled worker run

☐ Actions → **manual run** → Run workflow, ref `claude/vercel-deployment`, task `source-check`,
**dry run ticked**.

**Verified when:** the job is green and the summary shows discovery, readiness and the lease. A dry
run decides everything and writes nothing.

☐ Run it again with dry run **unticked**.

**Verified when:** the job is green; `publish status` in the log shows a dataset and a read model;
`publish verify` reports no missing files. Delivery is off in configuration, so nothing was sent.

### Stage D — the dashboard

☐ Create a Vercel project with root directory `web/`, framework preset Next.js.
☐ Set the environment variables in the table above. **Leave `AZMONITOR_CRON_ENABLED` unset and do
not create a dispatch token yet.**
☐ Deploy as a **preview**, not production.

**Verified when:** signing out and requesting any page redirects to `/login`; the overview shows
figures that match the reports the engine produced; a PDF, a PPTX and a workbook each download; and
the network tab shows no storage URL and no token.

### Stage E — recovery

☐ Run the manual workflow a second time without dry run.

**Verified when:** the second run reports `unchanged` and produces no new edition, every report from
the first run is still listed and still downloadable, and `verify` still reports no missing files.
This is the scenario that used to erase the archive.

### Stage F — scheduling (prepared, not activated)

Everything needed is in the branch. Activating it is two deliberate acts, in this order:

☐ Merge PR #2, which puts `scheduled.yml` on the default branch and **starts the cron schedule**.
☐ Optionally add the Vercel watchdog as a second opinion: `AZMONITOR_CRON_ENABLED=true` in
production only, plus a fine-grained PAT with *Actions: write* on this repository and nothing else.
☐ Delete `manual-run.yml`.

**Not yet done, and not to be done without your say-so.**

### Stage G — email (prepared, not sent)

Two separate switches, deliberately:

☐ Add the four `AZMONITOR_GRAPH_*` secrets.
☐ Set `enabled: true` in `config/delivery.yaml`.

Then send one edition to the single authorised recipient and confirm the ledger recorded `sent`
before widening the distribution list. WhatsApp stays disabled throughout; its implementation is
preserved for later.

**Not yet done. No email can be sent while either switch is off, and both are off.**

## Operating it

```bash
python -m azmonitor.cloud.publish status     # what the store and the read model hold
python -m azmonitor.cloud.publish verify     # catalogue and store agree
python -m azmonitor.cli delivery list        # the ledger, including anything needing a decision
```

**A delivery recorded as `needs_review` is never retried automatically.** A retry after an uncertain
send is how a report goes out twice. Resolve one deliberately:

```bash
python -m azmonitor.cli delivery resolve --id <delivery-id> --outcome sent|failed
```

## Rolling back

The Linux/Docker/systemd deployment in `deploy/` is untouched and still works. It runs the same
engine against the same configuration with a local dataset. To fall back, disable the workflow and
start the systemd timers; to go the other way, the reverse. The dataset format is the same in both,
so `publish save` from one and `publish restore` on the other moves it across.
