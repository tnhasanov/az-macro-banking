# Deploying the monitor to Vercel, Neon and GitHub Actions

Three pieces, set up in this order. Nothing here creates a paid resource: every service below is
used inside its free allowance at this workload, and `docs/costs.md` shows the arithmetic.

If you want the reasoning behind this shape rather than the steps, read
[`vercel-architecture.md`](vercel-architecture.md) first.

```
   Vercel Cron ─────► /api/cron/<task>  (watchdog: dispatches only a run that did not happen)
                              │
   GitHub Actions cron ───────┴──► scheduled.yml
                                        │  takes the database lease
                                        │  restores the dataset from Blob
                                        │  refresh → validate → readiness → report → deliver
                                        │  saves the dataset and new editions to Blob
                                        │  publishes the read model to Postgres
                                        ▼
                    Neon Postgres  ◄──── read model (27 MB) ────►  Vercel dashboard
                    Vercel Blob    ◄──── dataset + report archive ─┘  (authenticated downloads)
```

---

## 1. Neon Postgres

1. Create a project. Any region; `eu-central-1` keeps it near both the runner and the dashboard.
2. Copy the **pooled** connection string (it contains `-pooler`). The dashboard opens many short
   connections and the pooled endpoint is what serverless wants.
3. Nothing else. The worker creates its own schema on first run, and migrates it in place on later
   ones.

The read model is 27 MB at present, against a 0.5 GB free-tier allowance, and it is rebuilt from
the dataset rather than accumulated — it does not grow without bound.

## 2. Vercel Blob

Create a Blob store in the Vercel project and copy its `BLOB_READ_WRITE_TOKEN`.

The token is read-write and is the only credential that can reach the dataset. It belongs in GitHub
Actions secrets and in Vercel environment variables, nowhere else — in particular not in a URL,
where it would end up in a log. `VercelBlobStore` passes it in a header for exactly that reason.

## 3. The engine, on GitHub Actions

Set these as **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | What it is | Needed for |
|---|---|---|
| `AZMONITOR_DATABASE_URL` | Neon pooled connection string | the read model and the run lease |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob token | the dataset and the report archive |
| `AZMONITOR_ARCHIVE_BASE_URL` | public base of the dashboard, e.g. `https://monitor.example.com` | links in emails |
| `AZMONITOR_OWNER_EMAIL` | the one authorised test recipient | delivery routing |
| `AZMONITOR_GRAPH_TENANT_ID` | Entra tenant | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_ID` | app registration | Microsoft Graph email |
| `AZMONITOR_GRAPH_CLIENT_SECRET` | client secret | Microsoft Graph email |
| `AZMONITOR_GRAPH_SENDER` | mailbox to send as | Microsoft Graph email |

The four Graph secrets can be left unset until you authorise a live email test. Delivery is
disabled in `config/delivery.yaml` (`enabled: false`) regardless, so an unset credential is not
what is stopping mail from going out — the configuration is.

The workflow sets `AZMONITOR_PROFILE: neutral`. Leave it. It selects the distribution profile that
renders without the bank's logo, brand colours or name, and `publish save` **refuses to upload at
all** under a profile that does not permit external distribution. Removing that line does not
publish branded reports; it stops every run.

Run it once by hand before trusting the schedule: **Actions → scheduled → Run workflow**, task
`source-check`, **dry run ticked**. A dry run decides everything and produces and sends nothing, so
it exercises discovery, readiness and the lease without touching the store.

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

Then set the environment variables:

| Variable | Value | Notes |
|---|---|---|
| `AZMONITOR_DATABASE_URL` | Neon pooled string | read-only use; the dashboard never writes |
| `AZMONITOR_SESSION_SECRET` | `openssl rand -base64 48` | at least 32 characters, or the app refuses to serve |
| `AZMONITOR_DASHBOARD_PASSPHRASE_HASH` | the `pbkdf2$…` digest above | not the passphrase |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob token | to stream report files |
| `CRON_SECRET` | `openssl rand -hex 32` | Vercel sends this to the cron route |
| `AZMONITOR_OWNER_EMAIL` | your address | shown as the signed-in identity |
| `AZMONITOR_CRON_ENABLED` | **unset** | leave unset until you authorise scheduled dispatch |
| `GITHUB_DISPATCH_TOKEN` | fine-grained PAT, *Actions: write* on this repo only | only needed once dispatch is enabled |
| `GITHUB_REPOSITORY` | `owner/repo` | |
| `GITHUB_WORKFLOW_REF` | branch to dispatch, default `main` | |

**On a preview deployment, set none of the last four.** With `AZMONITOR_CRON_ENABLED` unset the
watchdog reports what it would have done and dispatches nothing, which is what a preview should do.

## 5. First run, in order

1. Run `scheduled` by hand with **dry run ticked**. Read the job summary.
2. Run it again without dry run. This is the run that first populates Blob and Postgres. It will
   take longer than later runs because there is nothing to restore.
3. Open the dashboard. If it says the read model is empty, step 2 did not complete — check the
   workflow log, not the dashboard.
4. Check `python -m azmonitor.cloud.publish status` in the workflow log: it prints what the store
   and the read model hold.
5. Leave the schedule to run for a few days and read the **System** page. Nothing is sent to anyone
   in this state.

## Enabling things later

Each of these is off, and each is off in a way that takes a deliberate action to change.

**Scheduled dispatch from the watchdog.** Set `AZMONITOR_CRON_ENABLED=true` in Vercel production
only. Actions cron already runs the schedule; this only adds the fallback.

**Real email.** Two separate switches, deliberately: `enabled: true` in `config/delivery.yaml`, and
the four Graph secrets. Send one edition to the single authorised recipient first and check the
delivery ledger recorded `sent` before widening the distribution list.

**WhatsApp.** The implementation is present and disabled in `config/delivery.yaml`. It stays that
way for this deployment.

## Rolling back

The Linux/Docker/systemd deployment in `deploy/` is untouched and still works. It runs the same
engine against the same configuration with a local dataset. To fall back, stop the GitHub schedule
(disable the workflow) and start the systemd timers; to go the other way, the reverse. The dataset
format is the same in both, so `publish save` from one and `publish restore` on the other moves it
across.
