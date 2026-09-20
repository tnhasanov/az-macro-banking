# Production readiness

**Verdict: not production-ready, and not because anything is broken.** Every component has been
built and exercised, but three of them have only ever been exercised against local stand-ins —
PostgreSQL 16 instead of Neon, a directory instead of Vercel Blob, and a local Next.js server
instead of a Vercel deployment. No Vercel or Neon credentials existed in the session that built
this, so no deployment was created and none could be.

The brief said not to declare production readiness on the strength of local tests, and this is what
that looks like when it is taken seriously. What follows separates what was actually verified from
what was not, and names exactly what is needed to close the gap.

---

## Verified, with evidence

### The engine, against the real dataset

| Check | Result |
|---|---|
| Python test suite | **151 passed, 3 skipped** |
| Cloud tests against real PostgreSQL 16 | **14 passed** |
| Dashboard test suite | **38 passed** (32 without a database, 6 skipped then) |
| Full source refresh | **645.8 s**, peak RSS 112 MB, exit 0 — ~300 documents from live sources |
| Monthly deck rendered end to end | 27 slides, PPTX + PDF + XLSX, from the real July 2026 dataset |
| Read model published | 57,974 indicators, 95 publications, 7 editions, 40 quality checks, 27 MB |
| Dataset round trip | 260 MB tarball, 396 files restored, digest verified, 520 report objects |

### The figures agree with the deck

Spot-checked against the rendered July 2026 edition, which is itself claim-validated against
sources:

| Figure | Dashboard | Dataset |
|---|---|---|
| Loans to the economy, July 2026 | 34,155.08 AZN mln | 34,155.081826338 |
| Loan-to-deposit ratio | 76.8% | 76.75 |
| FX share of deposits | 34.7% | 34.72 |
| Indicative pricing spread, AZN | +11.5 pp | 11.50 (AZN slice) |

### The security boundary

Each of these was exercised against a running server, not reasoned about:

| Attempt | Result |
|---|---|
| Any page without a session | 307 → `/login` |
| Any API route without a session | 401 |
| Cron endpoint with no bearer token | 401 |
| Cron endpoint with a wrong bearer token | 401 |
| Cron endpoint with a token that is a prefix of the real one | 401 (constant-time compare) |
| Wrong passphrase | 401, same message as every other failure |
| Report file listed by an edition | served, streamed through the app |
| `dataset/current.json` | 404 |
| `reports/../../dataset/current.json` | 404 |
| A plausible-but-unlisted report file | 404, indistinguishable from the others |

The file route authorises by **catalogue membership** — the key must appear in some edition's file
list — rather than by sanitising a path, and the storage URL is never handed to the browser.

### Concurrency and missed runs

| Scenario | Behaviour |
|---|---|
| Two workers race for the lease | one wins; the other is refused (tested on two connections) |
| A crashed run leaves its lease | taken over once lapsed, previous holder recorded |
| Watchdog while a lease is held | declines, names the holder and expiry |
| Watchdog, run on time | declines |
| Watchdog, run overdue | would dispatch — **blocked because dispatch is disabled** |
| Watchdog, repeated firing | one dispatch, then declines |
| Watchdog, unknown task | 404 |
| Watchdog, last run in the future (clock skew) | treated as on time, not a negative interval |
| Unchanged inputs | `unchanged`; no edition, no delivery |
| Failed blocking validation | edition blocked, reason recorded |
| Late weekly digest | still covers its own week |
| Asia/Baku → UTC across the year | fixed 4 hours, checked against tzdata in Jan/Apr/Jul/Oct |
| Schedule in three places | compared by `monitor schedule check`; CI fails on drift |

### Confidentiality

The brief forbade moving the bank's proprietary assets to personal cloud infrastructure. Verified on
real output:

| Artefact | Neutral profile | Branded profile |
|---|---|---|
| Deck embedded images | none | `ppt/media/image1.png` (the logo) |
| Brand colours in the deck | none | 289 × `6F00B6`, 3,936 × `DDD0EA`, and three more |
| Bank name in deck and PDF | absent | present |
| Workbook header colour | theme navy | brand purple |

The two profiles produce **identical figures**: all 207 fact-pack metrics equal, same edition
fingerprint, same reporting periods, same quality summary, same narrative. Only the cover differs.
`publish save` exits 3 and uploads nothing under a profile that does not permit external
distribution.

### The dashboard, looked at rather than assumed

Rendered in a real browser at 1440 px and 390 px, in light and dark mode: no console errors, no
page errors, no horizontal overflow on any page. Three defects were found this way and fixed —
`minmax(380px, …)` forcing a phone page 6–33 px wide, a missing icon causing a 404, and a
Recharts tooltip escaping the viewport.

---

## Not verified — and what each one needs

These are the gaps. None is known to be broken; all are untested against the real service.

| # | Gap | Why it matters | To close it |
|---|---|---|---|
| 1 | **Vercel Blob has never been contacted.** `VercelBlobStore`'s REST calls are exercised only against a local directory implementation of the same interface. | Wrong header, wrong status handling or an unexpected response shape would fail the first real save — after a 646-second refresh. | Set `BLOB_READ_WRITE_TOKEN` and run `publish save` once by hand. |
| 2 | **Neon has never been contacted.** Postgres was tested against a local PostgreSQL 16. | Neon's pooled endpoint runs pgbouncer in transaction mode. `prepare: false` is set for that reason but has not been proven against it. The lease uses a plain conditional insert, which is transaction-pooling-safe, but that is reasoning, not evidence. | Point `AZMONITOR_DATABASE_URL` at the pooled endpoint, run `publish readmodel`, then open the dashboard. |
| 3 | **Nothing has been deployed to Vercel.** `next build` succeeds locally and the output is 7 routes plus middleware; that is a build, not a deployment. | Middleware behaves differently on the Edge than in `next start`; environment variables, regions and the cron schedule are all deployment-time. | Deploy a preview with `AZMONITOR_CRON_ENABLED` unset. |
| 4 | **The GitHub Actions workflow has never run.** Its YAML is parsed and asserted by tests; no run has executed it. | Runner package availability, the pip cache, the lease step's exit-75 path and the job summary are unexercised. | Run it once with dry run ticked, then once without. |
| 5 | **The workflow-dispatch path is unexercised.** No `GITHUB_DISPATCH_TOKEN` has been issued. | A token with wrong scopes fails silently from the dashboard's point of view — the watchdog reports the status code and nothing else. | Issue a fine-grained PAT with *Actions: write*, then force a dispatch by temporarily lowering a threshold. |
| 6 | **No email has been sent.** Deliberate: the brief requires explicit authorisation first. | The Graph send path was tested against a fake provider, not Microsoft. | Authorise a controlled test to one recipient. |
| 7 | **Login rate limiting is per-instance and therefore weak.** Serverless instances do not share memory, so the 10-attempts-per-15-minutes limit applies per instance rather than globally. | An attacker distributing attempts across instances gets more tries than the limit suggests. The real brake is 600,000 PBKDF2 iterations plus a long passphrase. | Either accept it with a long passphrase, or move the counter into Postgres — which would give the dashboard its first write path, so it is a deliberate trade, not an oversight. |
| 8 | **Blob transfer volume is higher than it needs to be.** ~94 GB/month, about $4.70, because the whole 260 MB dataset moves both ways on every run even when only state changed. | Cost is minor; the wasted minute per run and the extra chance of a mid-transfer failure are the real cost. | Split the dataset into a small mutable part and an append-mostly raw archive. Described in `costs.md`. Not done here because it changes the storage layout and cannot be tested against real Blob from this environment. |
| 9 | **The dashboard has one reader and has not been load-tested.** | Not a risk at this scale; stated so it is not mistaken for a tested property. | — |

---

## What is deliberately switched off

Each of these is off in a way that takes a specific, separate action to change. None is off by
accident, and none can be turned on by an inherited or misread value.

| Off | Switch | Why it is off |
|---|---|---|
| Outbound delivery | `enabled: false` in `config/delivery.yaml` | the brief: no real email until explicitly authorised |
| WhatsApp | `channels.whatsapp.enabled: false` | the brief: preserved, disabled for this deployment |
| Scheduled dispatch from the watchdog | `AZMONITOR_CRON_ENABLED` unset | a preview must not run the engine |
| Upload under the branded profile | `AZMONITOR_PROFILE` + the `save` guard | the bank's assets do not belong in personal storage |
| Automatic retry of an uncertain delivery | `TERMINAL = ("sent",)`, no retry path from `needs_review` | a retry after an uncertain send is how a report goes out twice |

---

## Recommended order to reach production

1. Create the Neon project and the Blob store. Both stay inside their free tiers.
2. Set the GitHub secrets. Run `scheduled` with **dry run ticked**. Read the job summary. *(closes gap 4)*
3. Run it again without dry run. This is the first real write to Blob and Postgres. *(closes gaps 1, 2)*
4. Deploy the dashboard as a **preview**, with `AZMONITOR_CRON_ENABLED` unset and no dispatch token. Sign in, check every page against the engine's own output. *(closes gap 3)*
5. Watch the schedule for about a week. Nothing is sent to anyone in this state. Read the System page and the run history.
6. Only then: promote to production, issue the dispatch token and set `AZMONITOR_CRON_ENABLED=true`. *(closes gap 5)*
7. Separately, and only when you choose to: authorise one controlled email to one recipient, confirm the ledger recorded `sent`, and leave it there until you are satisfied. *(closes gap 6)*

Steps 1–6 create no paid resource and send nothing to anybody.

---

## One thing outside the scope of this work

`theme/assets/atb_logo.png` has been tracked in this **public** repository since its first commit,
along with the bank's brand colours and organisation name in `config/theme.yaml`. The distribution
profile added here keeps them out of anything rendered or uploaded by the cloud deployment, but it
cannot remove them from the repository's history, and purging git history is not something to do
without asking. Worth a decision: leave it, make the repository private (which costs Actions
minutes — see `costs.md`), or rewrite the history.
