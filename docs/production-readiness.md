# Production readiness

**Verdict: not production-ready, and the reason is unchanged — nothing has been run against the
real services.** Vercel, Neon and Vercel Blob have never been contacted from the environment this
was built in: there are no credentials for them, and `vercel.com` is unreachable through its egress
proxy. Everything below is either verified against a real PostgreSQL and the real 374 MB dataset, or
honestly marked as not verified.

What has changed since the last report is the quality of what is waiting to be deployed. An
independent review found four critical defects; all four were real, all four are fixed, and testing
them turned up four more.

---

## What the review found, and what was true

### 1. Private Blob access — confirmed, and worse than reported

The review said downloads were unauthenticated. They were, and reading the official SDK's source
showed the Python client was wrong in four other ways as well, so it could not have worked against
the real API at all:

| | Was | Actually |
|---|---|---|
| API base | `https://blob.vercel-storage.com` | `https://vercel.com/api/blob` |
| Upload | `PUT /<key>` | `PUT /?pathname=<key>` |
| API version | `7` | `12` |
| Access level | not sent | `x-vercel-blob-access: private` |
| Download | plain GET, no credential | `https://<store>.private.blob.vercel-storage.com/<path>` with a Bearer token |

**Fixed** by delegating to `@vercel/blob` — the SDK Vercel maintains — through
`tools/blob/blob.mjs`. Shelling out to Node from Python is a detour, and the deciding reason is the
dataset: 260 MB is not a single PUT, and multipart upload, retries and the error taxonomy are
exactly the undocumented behaviour worth not re-deriving.

Everything is written `access: 'private'`, so there is no public URL to leak. The dashboard reads
through `get(key, {access: 'private', token})` and streams the bytes; it checks the session in the
handler rather than trusting the middleware matcher alone; and it serves only pdf/pptx/xlsx keys
that an edition actually lists, so the dataset tarball, the delivery ledger and raw source documents
are unreachable even by someone signed in.

### 2. Historical report persistence — confirmed, and severe

The catalogue was rebuilt from `archive.index()`, which reads the local `outputs/` directory, and
published with `TRUNCATE editions`. Runner A publishes; its disk goes away; Runner B starts empty,
finds no local editions, truncates. Every report ever produced disappears from the dashboard while
its files sit untouched in the store.

**Fixed.** The catalogue is now one immutable JSON object per edition version, written to the store
beside the files it describes, and `publish_editions` upserts and never deletes. A side effect worth
noting: the catalogue now holds all **95** edition versions where the local index only ever reported
the 7 newest.

`tests/test_cloud_persistence.py::test_runner_b_does_not_erase_runner_as_history` is the scenario
the review asked for, end to end.

### 3. Failed-run handling — confirmed

The read-model step was gated on holding the lease, not on the task succeeding.

`failed_restore` is the one that matters. It means the dataset was never fetched, so the SQLite on
disk is empty — publishing indicators from it would have replaced every figure on the dashboard
with nothing. A storage failure turned into data loss.

**Fixed.** Publication is gated on outcomes 0 (ok) and 2 (partial) only. 3, 5 and 6 record the
failure separately for monitoring, leave the last good dataset alone, and fail the job with a
summary that says which outcome occurred and whether the dashboard was updated. Lock contention
(4) is still not a failure.

### 4. Watchdog reliability — confirmed, both parts

Elapsed-time thresholds cannot detect a specific missed occurrence. For a weekly report, any
threshold that does not fire on a healthy week also cannot detect a skipped Monday until the next
one.

**Fixed.** The watchdog enumerates the occurrences the schedule calls for, applies each one's grace,
and asks whether that occurrence was attempted — so a missed Monday digest is visible on Monday.
It distinguishes *satisfied*, *running* (due, unrecorded, lease held — late, not missing) and
*missed*, and a failed run counts as attempted so a broken cycle cannot become a dispatch loop.

The second part was the more dangerous one. `locks()` and the run history were wrapped in
`.catch(() => [])`, so a Neon outage read as "no lease is held" — the one conclusion that starts a
second worker on top of a healthy one. Both reads now propagate and the route answers 503 without
dispatching. **Verified by stopping PostgreSQL**: HTTP 503, `dispatched: false`.

### 5. Locking and recovery — confirmed

The job timeout is 60 minutes and the lease was 55. Renewal narrows that window but cannot close
it, because a process can be paused for longer than its remaining lease.

**Fixed with fencing.** Each acquisition bumps a monotonic fence token; the worker presents holder
and token before every persistent write, and once more immediately before the dataset pointer
moves — the write that would otherwise roll the dataset back to an older copy. Eleven tests in
`tests/test_lease_fencing.py` run against a real PostgreSQL, because the interesting behaviour is
the database arbitrating between two connections.

### 6. Initial dataset deployment — implemented

`publish seed` refuses a store that already holds a dataset, refuses a directory with no
`monitor.sqlite`, refuses to sweep up files that are not part of the dataset, and verifies the
upload by reading back what the store holds. `--with-reports` also uploads the existing archive so
the dashboard has history from the first run.

`restore` now distinguishes a genuinely empty store from one whose pointer has gone. The second is
damage, and treating it as a first run is how a backfill replaces a real dataset with an empty one.

### 7. Cost — measured, then acted on

The dataset splits 5.9 MB mutable / 254.2 MB static. A run that downloaded nothing was shipping all
260 MB back up. The static half is now re-uploaded only when its fingerprint changes: **260.0 MB →
5.9 MB, 11.5 s → 0.6 s**, measured on the real dataset. See `costs.md`.

### Four more, found while testing the above

- `publish_edition` silently dropped a manifest it skipped, so its uploaded and already-present
  lists did not account for every file in the edition.
- Two directories can claim the same version on disk (`v1_<stampA>`, `v1_<stampB>`, from repeated
  renders). Both were uploaded under one key, leaving the loser's files with no catalogue entry.
  `publish verify` went from 12 orphans to 0 on the real archive once the newest render became the
  published one.
- `_archive_editions` took an `ObjectStore` it never used, forcing `readmodel` to require storage it
  did not touch.
- The monitor task's times (07:45, 18:45) existed only in the systemd timer and the workflow, never
  in `config/schedule.yaml`. They are now in the configuration, and a test compares all four copies.

---

## Verified, with evidence

| Check | Result |
|---|---|
| Python test suite | **225 passed** (against real PostgreSQL 16) |
| Dashboard test suite | **59 passed**, 6 skipped without a database |
| Full source refresh | 645.8 s, peak RSS 112 MB, exit 0 — ~300 documents from live sources |
| Dataset seeded into an empty store | 260.0 MB, both halves verified by read-back |
| Restored on a worker with an empty filesystem | 396 files, both digests checked |
| Read model rebuilt there from nothing | 57,974 indicators, 95 editions, 95 publications |
| `publish verify` on the real archive | 226 files catalogued, 226 in store, 0 missing, 0 orphans |
| Upload after a run with no new documents | 5.9 MB, 0.6 s |
| Watchdog, live | named the exact occurrence ("13:15", "Monday 08:30", "07:45") |
| Watchdog, lease held | `state: running`, no dispatch |
| Watchdog, PostgreSQL stopped | **HTTP 503, `dispatched: false`** |
| Lease fencing | 11 tests across two real connections |

### The figures still agree with the deck

| Figure | Dashboard | Dataset |
|---|---|---|
| Loans to the economy, July 2026 | 34,155.08 AZN mln | 34,155.081826338 |
| Loan-to-deposit ratio | 76.8% | 76.75 |
| FX share of deposits | 34.7% | 34.72 |
| Indicative pricing spread, AZN | +11.5 pp | 11.50 (AZN slice) |

---

## Not verified — and what each one needs

| # | Gap | Why it matters | To close it |
|---|---|---|---|
| 1 | **Vercel Blob has never been contacted.** The SDK is exercised only against a local directory implementing the same interface. | The protocol is now the SDK's problem rather than ours, which is the point of using it — but "the SDK is correct" is a reasonable belief, not evidence. | `publish seed --check`, then `seed`, with a real token. |
| 2 | **Neon has never been contacted.** PostgreSQL 16 locally is not Neon. | Neon's pooled endpoint runs pgbouncer in transaction mode. `prepare: false` is set for that reason and the lease uses a plain conditional insert, which is transaction-pooling-safe — but that is reasoning, not evidence. | Point `AZMONITOR_DATABASE_URL` at the pooled endpoint and run `publish readmodel`. |
| 3 | **Nothing has been deployed to Vercel.** `next build` succeeds; that is a build, not a deployment. | Middleware behaves differently on the Edge than under `next start`; environment variables, regions and the cron schedule are deployment-time. | Deploy a preview with `AZMONITOR_CRON_ENABLED` unset. |
| 4 | **The workflow has never run.** Its YAML is parsed and its gating asserted by 24 tests; no run has executed it. | Runner package availability, the pip and npm caches, the exit-75 lease path and the job summary are unexercised. | Run it once with dry run ticked, then once without. |
| 5 | **The dispatch path is unexercised.** No `GITHUB_DISPATCH_TOKEN` has been issued. | A token with wrong scopes fails silently from the dashboard's point of view. | Issue a fine-grained PAT with *Actions: write*, then force a dispatch. |
| 6 | **No email has been sent.** Deliberate. | The Graph send path was tested against a fake provider, not Microsoft. | Authorise a controlled test to one recipient. |
| 7 | **Login rate limiting is per-instance and therefore weak.** Serverless instances do not share memory. | An attacker distributing attempts across instances gets more tries than the limit suggests. The real brake is 600,000 PBKDF2 iterations plus a long passphrase. | Accept it with a long passphrase, or move the counter into Postgres — which would give the dashboard its first write path, so it is a deliberate trade. |
| 8 | **The raw archive is still downloaded in full on every run** (46.8 GB/month). | Skipping it would mean the worker cannot see documents it may need to re-parse. The upload side was the safe half to optimise. | Would need a behavioural change validated against live sources. |
| 9 | **The dashboard has one reader and has not been load-tested.** | Not a risk at this scale; stated so it is not mistaken for a tested property. | — |

---

## What is deliberately switched off

| Off | Switch | Why |
|---|---|---|
| Outbound delivery | `enabled: false` in `config/delivery.yaml` | no real email until explicitly authorised |
| WhatsApp | `channels.whatsapp.enabled: false` | preserved, disabled for this deployment |
| Watchdog dispatch | `AZMONITOR_CRON_ENABLED` unset | a preview must not run the engine |
| Upload under the branded profile | `AZMONITOR_PROFILE` + the `save` guard | the bank's assets do not belong in personal storage |
| Automatic retry of an uncertain delivery | `TERMINAL = ("sent",)` | a retry after an uncertain send is how a report goes out twice |

---

## Confidentiality: what the repository holds

Re-scanned across all 193 tracked files.

**No credentials.** The only credential-shaped string is a test fixture in `web/tests/auth.test.ts`.

**Three binary fixtures** (`tests/fixtures/*.xlsx`) are Central Bank published tables — Cədvəl 2.6,
3.2.1 and 5.6 — which are public source data, not bank-internal material.

**One proprietary asset**, unchanged from the last report and still needing your decision:
`theme/assets/atb_logo.png` has been tracked in this **public** repository since its first commit,
alongside the bank's brand colours and organisation name in `config/theme.yaml`.

The neutral profile keeps all of it out of anything rendered or uploaded by the cloud deployment —
verified on real output: the neutral deck, workbook and PDF contain no embedded image, no brand
colour and no mention of the bank, while the branded ones contain all three, and all 207 fact-pack
metrics are identical between the two profiles. What the profile cannot do is remove the logo from
the repository's history.

Three options, none of which I have taken:

1. **Leave it.** The logo is already public and has been since the first commit.
2. **Make the repository private.** This costs roughly **$18.72/month** in Actions minutes, which
   are free only for public repositories (see `costs.md`) — a change since 1 January 2026.
3. **Rewrite history.** Removes it properly, breaks every existing clone and commit reference.

This needs an ownership decision from you, not a default from me.

---

## Recommended order to reach production

Set out in full in [`cloud-deployment.md`](cloud-deployment.md). In short: Neon and Blob (free
tiers) → secrets → seed and verify the dataset → a dry run → a real run with delivery off → watch
it for a few days → merge to the default branch to start the schedule → and only then, separately,
authorise one email.

Stages one to five create no paid resource and send nothing to anybody.
