# Production readiness

## Status after the report-jobs work (2026-09-26)

The two user journeys — request a report in the dashboard, follow it, download it and have it
emailed; and automatic source checks that publish validated editions and email subscribers — are
implemented end to end and proven on this machine against the real engine, the live CBA website,
Postgres, the production build of the dashboard and a real browser. See
[`report-jobs.md`](report-jobs.md) for how it works and how to run it, and
[`e2e-report.md`](e2e-report.md) for the sixteen checks and their evidence.

What has **not** happened is a run against the real cloud services. From this environment Vercel,
Vercel Blob, Neon and Resend are unreachable (connections refused by the network policy; only GitHub
is reachable), and the last cloud worker run (21 September) stopped at the Blob credential because
the store is not available to the Development environment the minted token is for. The remaining
steps are the user actions listed at the end of `report-jobs.md`'s setup section and in the
handover summary; none of them is a code change.

The rest of this page is the earlier assessment, kept for its record of what was found and fixed.

---


**Verdict: the code is materially better than it was, and still nothing has run against the real
services.** This attempt tried to change that and could not. The reason is worth stating precisely,
because it is not the one from last time.

## Why cloud integration testing did not happen

Not missing credentials. **The environment's network policy denies the connection.**

```
$ curl https://vercel.com/api/blob                → 000
$ curl https://api.vercel.com/v2/user             → 000
$ curl https://blob.vercel-storage.com            → 000
$ curl https://console.neon.tech                  → 000
$ curl https://api.github.com                     → 200

$ curl "$HTTPS_PROXY/__agentproxy/status"
  "recentRelayFailures": [
    { "kind": "connect_rejected",
      "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
      "host": "vercel.com:443" },
    ... api.vercel.com:443, blob.vercel-storage.com:443, console.neon.tech:443
  ]
```

The allowed set is GitHub, npm, PyPI and Anthropic. Handing over credentials would not help, and
you should not: there is nothing for them to authenticate against from here.

**What would unblock it:** the remote environment's network egress policy has to allow
`vercel.com`, `api.vercel.com`, `*.blob.vercel-storage.com` and the Neon endpoints, with the
credentials supplied through the environment's own secret configuration rather than pasted into a
conversation. That is a setting on the Claude Code environment, not something the code can change.

Connectors available in this session: Gmail and Google Calendar. There is no Vercel connector and
no Neon connector, so there is no second route to those services either.

## What was done instead

Everything that does not require reaching Vercel or Neon — which turned out to include finding four
more defects, two of which would have broken the first real run.

| | |
|---|---|
| Blob client checked against the installed SDK | **2 defects found and fixed** |
| What seeding actually uploads, audited byte by byte | **1 defect found and fixed** |
| The deployment sequence, checked against GitHub's real behaviour | **1 defect found and fixed** |
| Failure and recovery scenarios | 5 more added, all passing |
| Python tests | **250 passed** |
| Dashboard tests | **59 passed** |
| Blob conformance tests | **7 passed** |

---

## What this round found

### A. The seeding procedure uploaded branded material, and the profile guard approved it

The distribution profile governs how the engine *renders*. It says nothing about files rendered
earlier under a different profile — and an archive of those is exactly what a first seed uploads.

`publish seed --with-reports` under the neutral profile, against the archive this repository
actually holds, uploaded **95 branded decks, 36 branded workbooks and 95 branded PDFs** to personal
object storage. Every one carried the bank's logo or its palette. The guard passed each time,
correctly, because the profile was neutral. The files were not.

**Fixed** by inspecting the artefacts themselves. Every file is opened and read before upload, and
the question asked is "does this file carry the organisation's identity?" — matched against markers
declared in `config/distribution.yaml`: the name in extracted text, the palette as Office XML spells
it and as a PDF's content stream draws it, and, in a deck or a workbook, any embedded image at all.
A file that cannot be opened is a finding, not a pass. A refusal stops the whole publish rather than
skipping one edition, because a half-uploaded archive with no record of which half is worse than
none.

Verified on the real archive: the same seed now uploads zero report files and refuses with the
reason, while still seeding the dataset. A neutral edition — rendered, converted to PDF and
inspected end to end — passes cleanly.

The PDF half of that check was weaker than the rest until CI said so, twice over.

* It only read text, and a converted deck keeps the logo and the palette without necessarily
  keeping the footer that names the bank. 78 of the 95 PDFs named the organisation; the other 17
  carried its logo on the cover and its brand purple on every slide, and passed. Reading colours as
  well as text catches all 95.
* It extracted that text by running `pdftotext`, a binary nothing declared as a dependency. The
  machine these tests were written on had poppler installed; the CI runner did not, so the guard
  reported every PDF as uninspectable there and the suite failed on four tests. Worse than the
  failure was the passing case: `pdftotext` reports a file it cannot parse by exiting non-zero with
  empty output, which read as "no organisation named" — a fail-open in a guard whose whole premise
  is that "we could not check" is not "it is clean".

Both are fixed by reading the PDF in-process with `pdfplumber`, already a dependency of the engine
because the parsers read published PDFs with it. A file it cannot open raises, and an exception was
already a finding. A test asserts the inspector shells out to nothing, so the dependency cannot
drift back in unnoticed.

### B. `multipart` is opt-in, and the client was not passing it

The SDK streams a single request unless told otherwise. A 260 MB dataset upload was therefore one
request with no per-part retry, and a failure at 95% would have restarted from nothing — the
opposite of the reason given for adopting the SDK. **Fixed** with a threshold at 8 MB, the SDK's own
part size.

### C. `BlobNotFoundError` does not set `.name`

An instance reports `"Error"`; only `constructor.name` says otherwise. The existence check tested
`error?.name === "BlobNotFoundError"`, which is always false, so a missing object raised a storage
failure instead of answering "not there". `publish_edition` asks exactly that question before
writing anything, **so the first upload of every edition would have failed.** Matched by `instanceof`
now, and pinned by a test.

### D. The documented deployment sequence was not executable

It said to run the scheduled workflow by hand before merging. GitHub registers a workflow from the
default branch: until `scheduled.yml` is on `main` it has no id and cannot be dispatched at all.
Confirmed against this repository — the Actions API lists `checks.yml` and nothing else.

Merging it to get the button would also arm its cron, because `schedule:` fires from the default
branch and nowhere else. **Fixed** with `manual-run.yml`: `workflow_dispatch` only, no schedule
trigger and never one, checks out whichever branch you name, defaults to a dry run, shares the
scheduled workflow's concurrency group, and refuses to start if delivery has been switched on.

---

## What the previous review found

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

| # | Gap | Blocked by | To close it |
|---|---|---|---|
| 1 | **Vercel Blob has never been contacted.** | Network policy denies `blob.vercel-storage.com` and `vercel.com`. | Allow those hosts, then `publish seed --check` with a real token. |
| 2 | **Neon has never been contacted.** PostgreSQL 16 locally is not Neon. | Network policy denies `console.neon.tech`. | Allow it, point `AZMONITOR_DATABASE_URL` at the pooled endpoint, run `publish readmodel`. |
| 3 | **Nothing has been deployed to Vercel.** | No Vercel connector; `api.vercel.com` denied. | Allow it and connect a Vercel integration, or deploy from your own machine. |
| 4 | **The workflow has never run.** | `scheduled.yml` is not on the default branch, so GitHub cannot dispatch it. | Commit `manual-run.yml` to `main` — one file. Everything else is ready. |
| 5 | **The dispatch path is unexercised.** | No `GITHUB_DISPATCH_TOKEN`. | Issue a fine-grained PAT with *Actions: write*, at stage F. |
| 6 | **No email has been sent.** | Deliberate; both switches off. | Stage G, on your authorisation. |
| 7 | **Login rate limiting is per-instance.** | Serverless instances share no memory. | Accept with a long passphrase, or move the counter into Postgres — which would give the dashboard its first write path, so it is a trade rather than a fix. |
| 8 | **The multipart wire path is asserted indirectly.** | It does not route through the dispatcher MockAgent installs. | A real store. The decision and threshold are pinned; the transfer itself is not. |
| 9 | **The raw archive is still downloaded in full each run** (46.8 GB/month). | Skipping it would change what the engine can see. | Would need validation against live sources. |

## What is deliberately switched off

| Off | Switch | Why |
|---|---|---|
| Outbound delivery | `enabled: false` in `config/delivery.yaml` | no real email until explicitly authorised |
| WhatsApp | `channels.whatsapp.enabled: false` | preserved, disabled for this deployment |
| Watchdog dispatch | `AZMONITOR_CRON_ENABLED` unset | a preview must not run the engine |
| Upload under the branded profile | `AZMONITOR_PROFILE` + the `save` guard | the bank's assets do not belong in personal storage |
| Automatic retry of an uncertain delivery | `TERMINAL = ("sent",)` | a retry after an uncertain send is how a report goes out twice |

---

## Confidentiality: what the repository holds, and what seeding sends

### The repository

Re-scanned across all tracked files. **No credentials** — the only credential-shaped string is a
test fixture. **Three binary fixtures** (`tests/fixtures/*.xlsx`) are Central Bank published tables
(Cədvəl 2.6, 3.2.1, 5.6), public source data. **One proprietary asset**:
`theme/assets/atb_logo.png`, tracked in this **public** repository since the first commit, with the
brand colours and organisation name in `config/theme.yaml`.

### What seeding actually sends

Audited by reading the artefacts rather than trusting the profile:

| What travels | Finding |
|---|---|
| 383 raw source documents | All from `www.cbar.az`, `uploads.cbar.az`, `www.stat.gov.az`. Public government sources, no bank documents. |
| `monitor.sqlite` (14 tables) | Observations, publications, passages, policy decisions — all derived from those sources. |
| `deliveries.sqlite` | **Empty.** No recipients, no send history. |
| State files | No credential-shaped keys in any of them. |
| Email addresses in the dataset | One: `mail@cbar.az`, printed in the footer of the Central Bank's own bulletins. |
| `report_editions.path` | Absolute local paths. Discloses a directory layout, nothing of the bank's. |
| The report archive | **Refused.** Every existing edition carries the bank's identity; see finding A. |

So the dataset is safe to seed and the archive is not, which is now enforced rather than assumed.

### The decision that is yours

The logo. The neutral profile keeps it out of everything rendered, and the artefact guard now keeps
it out of everything uploaded. Neither can remove it from the repository's history.

1. **Leave it.** Already public since the first commit.
2. **Make the repository private.** Costs roughly **$18.72/month** in Actions minutes — free only
   for public repositories, and since 1 January 2026 there is an additional $0.002/minute platform
   charge that also excludes public repositories. See `costs.md`.
3. **Rewrite history.** Removes it properly; breaks every existing clone and commit reference.

Cost should not decide this. Whether the asset is yours to publish should, and that is not a
question I can answer for you.

## Recommended order to reach production

Set out in full in [`cloud-deployment.md`](cloud-deployment.md). In short: Neon and Blob (free
tiers) → secrets → seed and verify the dataset → a dry run → a real run with delivery off → watch
it for a few days → merge to the default branch to start the schedule → and only then, separately,
authorise one email.

Stages one to five create no paid resource and send nothing to anybody.
