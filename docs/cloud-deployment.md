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

### Two GitHub rules that decide when this can work

**A `schedule:` trigger only fires from the default branch.** A workflow sitting on
`claude/vercel-deployment` will never run on its own, however correct its cron lines are. Until the
branch is merged, every run has to be started by hand with **Run workflow** — which is the right
posture for the testing stages below anyway.

**Scheduled workflows are disabled after 60 days without repository activity.** That is one of the
two failure modes the Vercel watchdog exists to catch; the other is GitHub's cron being
best-effort under load.

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

Each stage adds exactly one capability. Nothing skips ahead.

### Stage 1 — code integration (no data, no runs)

Merge, or don't: the dashboard can be deployed as a preview from the branch. What matters is that
merging the workflow to the default branch is what makes the schedule live, so treat that merge as
the moment scheduling begins — not as a code-review formality.

Before merging, confirm the safety switches are where you expect:

```bash
grep -n "enabled:" config/delivery.yaml          # expect: enabled: false
grep -n "AZMONITOR_PROFILE" .github/workflows/scheduled.yml   # expect: neutral
```

A merge cannot start sending email. Delivery is off in configuration, and the four Graph secrets are
unset. It *can* start the cron schedule, which is why stage 5 is where it belongs.

### Stage 2 — seed the dataset

The validated dataset is 374 MB of SQLite and downloaded documents and is not in git, so it has to
come from the machine that holds it. From that machine:

```bash
export BLOB_READ_WRITE_TOKEN=…            # the private store
export AZMONITOR_PROFILE=neutral

python -m azmonitor.cloud.publish seed --check           # verifies, uploads nothing
python -m azmonitor.cloud.publish seed --with-reports    # dataset + the existing report archive
```

`seed` refuses a store that already holds a dataset, refuses a directory with no `monitor.sqlite`,
refuses to sweep up files that are not part of the dataset, and verifies the upload by reading back
what the store now holds. `--with-reports` also uploads the existing editions and their catalogue,
so the dashboard has history from the first run rather than an empty archive.

Then confirm it round-trips onto a machine that has never seen it:

```bash
AZMONITOR_DATA_DIR=/tmp/fresh/data python -m azmonitor.cloud.publish restore
python -m azmonitor.cloud.publish readmodel
python -m azmonitor.cloud.publish verify        # every catalogued file must be in the store
```

### Stage 3 — a dry run

**Actions → scheduled → Run workflow**, task `source-check`, **dry run ticked**. A dry run decides
everything and produces and sends nothing, so it exercises discovery, readiness, the lease and the
fence without touching the store.

Read the job summary. It names the outcome and says in plain words whether the dashboard was
updated.

### Stage 4 — real processing, still no delivery

Run it again without dry run. This is the first run that writes to Blob and Postgres.

Then open the dashboard and check its figures against the reports the engine already produced. If
the dashboard says the read model is empty, this step did not complete — read the workflow log, not
the dashboard.

Leave the schedule off and watch a few manual runs. Nothing is sent to anyone in this state.

### Stage 5 — production scheduling

Merge to the default branch, which makes the `schedule:` triggers live. Optionally also enable the
Vercel watchdog as a second opinion: set `AZMONITOR_CRON_ENABLED=true` in production only, and give
the deployment a `GITHUB_DISPATCH_TOKEN` with *Actions: write* on this repository and nothing else.

### Stage 6 — controlled email delivery

Two separate switches, deliberately: `enabled: true` in `config/delivery.yaml`, and the four Graph
secrets. Send one edition to the single authorised recipient, confirm the ledger recorded `sent`,
and leave it there until you are satisfied.

WhatsApp stays disabled throughout. Its implementation is preserved in `config/delivery.yaml` for
later activation.

---

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
