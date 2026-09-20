# Where each piece runs, and why

The brief asked for a Vercel-native deployment and said to recommend a separate worker only if the
Vercel-native architecture proved technically unsuitable. This is the evidence that decision rests
on, including a measurement that overturned an earlier conclusion of mine.

**Summary.** Vercel runs the dashboard and the schedule watchdog. The reporting engine runs on a
GitHub Actions runner. Postgres holds a derived read model, Vercel Blob holds the dataset and the
report archive. The engine's own code, calculations, validation, templates, delivery ledger and
publication-detection rules are unchanged — what changed is where the process runs and where its
files live.

---

## The claim I got wrong first, and the correction

My first analysis rejected a Vercel Function on **bundle size**: roughly 420 MB of Python
dependencies plus 232 MB of LibreOffice against a 500 MB limit. That reasoning does not survive
contact with the current platform.

- The Python Functions bundle limit is 500 MB, raised from 250 MB — but **with Fluid compute and
  Active CPU enabled, a Function may be up to 5 GB uncompressed.** 652 MB fits inside that with
  room to spare.
- LibreOffice cannot be `apt-get install`ed into the managed Python runtime, but Vercel **does**
  support deploying a Python app as a **container image**, where the Dockerfile installs
  operating-system packages and Vercel builds and runs the image as a Function. LibreOffice is the
  documented example of exactly this case.

So the honest position is: **a Vercel-native deployment is technically possible.** It was rejected
on a different constraint, set out below, and it is worth being precise about which one, because
the wrong reason would send someone down the wrong path later.

## The constraint that actually decides it: the duration budget

A Vercel Function — container image or not — is still one invocation with one wall-clock ceiling.

| Limit | Value |
|---|---|
| maxDuration, Pro, Fluid compute, generally available | **800 s** |
| maxDuration, extended tier | 1800 s, **beta**, per-function configuration, specific runtime versions only |
| Cron retry on failure | **none** — Vercel does not retry a failed cron invocation |

Against that, the measured work of one full cycle on this dataset:

| Step | Measured | Notes |
|---|---|---|
| Full source refresh | **645.8 s**, peak RSS 112 MB | measured 20 Sep 2026 on a warm dataset; ~300 documents from cba.az and stat.gov.az |
| Render monthly deck (27 slides) | ~1.2 s | native charts, no images |
| PPTX → PDF via LibreOffice | ~19.7 s | first conversion in a cold process; includes profile creation |
| Dataset restore from object storage | ~10–15 s | 260 MB tarball |
| Dataset save to object storage | ~13 s | measured against a local store; network adds to this |

The refresh dominates, and it is dominated in turn by **roughly three hundred sequential HTTPS
fetches from two government web servers whose latency is not ours to control.** That is the part
that makes the budget untenable.

Adding it up: 646 s of refresh, ~13 s to restore the dataset, 1.2 s to render, 19.7 s for the PDF
and ~13 s to save comes to **about 693 s against an 800-second ceiling — 87% of the budget, on a
run where nothing went wrong.** The refresh was measured on a warm dataset with most documents
already downloaded; a month-end where every source has published is slower, and the variance lives
entirely in someone else's web server. The consequence of overrunning is specific and bad:

- the invocation is killed with no retry;
- the engine's `save` step never runs, so the refreshed dataset is lost and the next run redoes it;
- the run lease is held until it expires rather than being released, so the next scheduled run
  declines as well.

One slow morning on a source website therefore costs two cycles, not one. The 1800-second tier
would give headroom, but it is in beta, and putting the only path to a report on a beta limit whose
failure mode is silent data loss is not a trade worth making for this system.

## What the alternative gives up, and what it costs

| | Vercel Function (container image) | GitHub Actions runner |
|---|---|---|
| Wall clock | 800 s GA / 1800 s beta | 6 hours |
| System packages | via Dockerfile | `apt-get`, directly |
| Disk | `/tmp`, ephemeral | 14 GB workspace |
| Retry on failure | none | re-run a job, or the watchdog dispatches |
| Concurrency control | own implementation | `concurrency:` group **and** the database lease |
| Cost for this workload | Active CPU $0.128/h + memory $0.0106/GB-h | **free** — public repository |

The engine is a batch job that runs six times a day and takes about twelve minutes. That is what a
CI runner is for. The dashboard is a set of short, bursty, read-only requests against a small
Postgres projection. That is what a Vercel Function is for. Splitting them puts each workload on
the thing shaped like it, and neither piece is contorted to fit.

## What Vercel still does

Vercel is not reduced to static hosting here. It runs:

- **the dashboard** — five server-rendered pages, an authenticated file-download route that streams
  report files out of Blob without ever handing the browser a storage URL, and the sign-in route;
- **the schedule watchdog** — `/api/cron/<task>`, on Vercel Cron. Actions cron is the primary
  trigger; the watchdog checks whether the run that should have happened did, and dispatches one
  only when it did not. This is not redundancy for its own sake: GitHub disables scheduled
  workflows in a repository with no activity for sixty days, and Actions cron is explicitly
  best-effort under load. Two independent schedulers, neither of which can double-fire, because the
  watchdog declines while a lease is held and the workflow has its own concurrency group.

## Persistence: what was evaluated, and what was not migrated

The brief said not to migrate the database for architectural elegance, and to evaluate a simpler
approach first. That is what happened.

**The engine still uses SQLite, unchanged.** It remains the authority for every observation,
vintage, publication record, provenance row, edition, fingerprint and quality result. The pipeline
code was not touched. What made this work in a cloud runner is that the engine already had
`AZMONITOR_RESTORE_CMD` and `AZMONITOR_SAVE_CMD` hooks, called inside its own job lock: the dataset
is pulled from object storage before the cycle and pushed back after it, and the engine neither
knows nor cares that the storage is remote.

**Postgres carries two things only**, and both are things SQLite-in-a-tarball genuinely cannot do:

1. **A derived read model.** The dashboard cannot open a 374 MB SQLite file over HTTP on every page
   view. The worker projects what the dashboard needs — 57,974 indicator rows, publications,
   editions, run history, quality results, a delivery mirror — into 27 MB of Postgres. It is
   rebuilt from the dataset on every run and can be dropped at any time without losing anything. A
   figure that disagrees between the two is a projection bug, never a data question.
2. **A distributed lease.** A file lock cannot coordinate two runners. `job_locks` uses a
   conditional insert (`ON CONFLICT ... WHERE expires_at < now()`) so exactly one holder wins, and
   a lease left behind by a crashed run is taken over once it lapses.

**Vercel Blob holds** the dataset tarball and every published report version. Editions are
immutable: `publish_edition` refuses to overwrite a key that exists, so a link sent six months ago
still resolves to the bytes that were sent. The dataset pointer moves only *after* the new tarball
has uploaded and its digest is recorded, so a run that dies mid-upload leaves an orphan object and
a pointer still naming the last complete dataset.

Nothing persistent lives in a Function's temporary directory.

## The schedule

Cron is UTC everywhere. Asia/Baku is UTC+4 all year with no daylight saving, so the conversion is a
fixed four hours and the local times in `config/schedule.yaml` are exact. This is verified against
the tzdata database in January, April, July and October by a test, not assumed.

| Task | Asia/Baku | UTC (Actions cron) | Watchdog (Vercel cron) |
|---|---|---|---|
| source check | 09:15, 13:15, 17:15 | 05:15, 09:15, 13:15 | 05:45, 13:45 |
| weekly digest | Monday 08:30 | Monday 04:30 | Monday 06:00 |
| monitor | 07:45, 18:45 | 03:45, 14:45 | 15:15 |

The schedule is now written in three places — `config/schedule.yaml`, the systemd timers and the
workflow — so `monitor schedule check` compares all three and CI fails if any drifts.

## Concurrency, idempotency and missed runs

Cron delivery is at-least-once on every platform, so none of this relies on a trigger firing exactly
once.

- **Two runs at once.** The workflow's `concurrency` group queues the second; the database lease
  refuses it outright if it gets that far. The workflow exits 75 (`EX_TEMPFAIL`) rather than failing
  when the lease is held, so a skipped run does not look like a broken one.
- **A repeated cron event.** The watchdog declines while a lease is held, and declines again once
  the run has recorded itself.
- **A crashed run.** Its lease lapses and the next run takes it over, recording who held it before.
- **A delayed run.** The weekly digest measures its window from its own Sunday, not from the moment
  it happens to run, so a digest produced late still covers the week it is for.
- **Unchanged data.** The edition fingerprint covers inputs, narrative and configuration. Identical
  inputs return `unchanged` and no report is produced and nothing is sent.
- **Failed validation.** A blocking quality failure stops the edition. It does not produce a report
  with a warning on it.
- **An uncertain delivery.** It is recorded as `needs_review` and is *never* retried automatically,
  because a retry after an uncertain send is how a report goes out twice. Resolving one is a
  deliberate command-line action against the ledger; the dashboard can display these but cannot act
  on them.

## Sources

- [Vercel Functions limits](https://vercel.com/docs/functions/limitations)
- [Python Functions bundle size limit increased to 500 MB](https://vercel.com/changelog/python-vercel-functions-bundle-size-limit-increased-to-500mb)
- [Fluid compute](https://vercel.com/docs/fluid-compute)
- [Configuring maximum duration](https://vercel.com/docs/functions/configuring-functions/duration)
- [Deploy Python apps on Vercel using Docker](https://vercel.com/kb/guide/vercel-docker-python-apps)
- [Cron jobs](https://vercel.com/docs/cron-jobs) — no retry on failure
- [Active CPU pricing for Fluid compute](https://vercel.com/blog/introducing-active-cpu-pricing-for-fluid-compute)
- [Vercel Blob usage and pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing)
- [Neon pricing](https://neon.com/pricing)
