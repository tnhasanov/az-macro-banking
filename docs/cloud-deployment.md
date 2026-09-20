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

Create a Blob store in the Vercel project and copy its `BLOB_READ_WRITE_TOKEN`.

**Everything is written with `access: 'private'`.** A private blob has no publicly readable URL at
all: reads carry the token in an Authorization header. That is why the dashboard streams report
files through its own authenticated route rather than redirecting to storage — a URL copied out of
the network tab is worth nothing to anyone who is not signed in.

The token is read-write and is the only credential that can reach the dataset. It belongs in GitHub
Actions secrets and Vercel environment variables, nowhere else — in particular never in a URL,
where it would end up in a log.

## 3. The engine, on GitHub Actions

Set these as **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | What it is | Needed for |
|---|---|---|
| `AZMONITOR_DATABASE_URL` | Neon pooled connection string | the read model and the run lease |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob token | the dataset and the report archive |
| `AZMONITOR_ARCHIVE_BASE_URL` | public base of the dashboard | links in emails |
| `AZMONITOR_OWNER_EMAIL` | the one authorised test recipient | delivery routing |
| `AZMONITOR_GRAPH_TENANT_ID` | Entra tenant | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_ID` | app registration | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_SECRET` | client secret | Microsoft Graph email |
| `AZMONITOR_GRAPH_SENDER` | mailbox to send as | Microsoft Graph email |

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

**This is the one thing that needs you before anything else can proceed**, because it is a commit to
the default branch and I do not push there. It is a single new file and touches nothing else.

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
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob token | to stream report files out of private storage |
| `CRON_SECRET` | `openssl rand -hex 32` | Vercel sends this to the cron route |
| `AZMONITOR_OWNER_EMAIL` | your address | shown as the signed-in identity |
| `AZMONITOR_CRON_ENABLED` | **unset** | leave unset until you authorise scheduled dispatch |
| `GITHUB_DISPATCH_TOKEN` | fine-grained PAT, *Actions: write* on this repo only | only once dispatch is enabled |
| `GITHUB_REPOSITORY` | `owner/repo` | |
| `GITHUB_WORKFLOW_REF` | branch to dispatch, default `main` | |

**On a preview deployment, set none of the last four.** With `AZMONITOR_CRON_ENABLED` unset the
watchdog reports what it would have done and dispatches nothing.

---

## The staged sequence

Seven stages. Each adds exactly one capability, each has something you can look at to know it
worked, and nothing skips ahead. **Stages A to E send nothing to anybody and create no paid
resource.**

Throughout: ☐ = needs you, ☑ = already done and in the branch.

### Stage A — infrastructure

☐ Create a Neon project and copy the **pooled** connection string (it contains `-pooler`).
☐ Create a Vercel Blob store. It must be a **private** store; do not make it public to make
downloads work.
☐ Add both, plus `AZMONITOR_OWNER_EMAIL`, as repository secrets in GitHub → Settings → Secrets.
☐ Commit `.github/workflows/manual-run.yml` to `main`. One file, nothing else; see above for why.

**Verified when:** the Actions tab lists a *manual run* workflow with a **Run workflow** button.

☑ Everything that has to exist in the repository for this to work is on the branch already.

### Stage B — seed the data

From the machine that holds the validated dataset, with `BLOB_READ_WRITE_TOKEN` set:

```bash
export AZMONITOR_PROFILE=neutral
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
