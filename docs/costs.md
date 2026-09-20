# What this costs to run

Estimated from the measured workload, at the pricing in effect in September 2026. Every figure
traces to something measured in this repository rather than to a guess about usage.

**Nothing here requires a new paid resource.** The line worth watching was Blob data transfer, and
measuring it led to a change that cut it by most of its value; the arithmetic is below.

## The workload, measured

| Quantity | Measured | Where it comes from |
|---|---|---|
| Runs per day | 6 | three source checks, two monitors, one weekly digest on Mondays |
| Dataset on disk | 374 MB | SQLite 35 MB, raw source documents 292 MB, state 1.7 MB |
| Dataset compressed | **260.0 MB** | one `save` of the real dataset |
| — the half that changes every run | **5.9 MB** | databases + state |
| — the half that rarely changes | **254.2 MB** | 383 downloaded source documents |
| Report archive | 226 files over 95 edition versions | `publish verify` against the real archive |
| Read model in Postgres | 27 MB | 57,974 indicator rows plus publications, editions, runs, quality |
| Full source refresh | 645.8 s, peak RSS 112 MB | measured 20 Sep 2026 |
| Dashboard page views | a handful a day | one reader |

## Blob transfer: what measuring it changed

The engine restores the dataset before each cycle and saves it after, inside its own lock. Shipping
all 260 MB both ways, six times a day, is 94 GB a month — and almost all of it was the *same 254 MB
of source documents*, re-uploaded after runs that had downloaded nothing.

The dataset is now two objects, and the static half is re-uploaded only when its fingerprint
changes. Measured on the real dataset:

| | Before | After |
|---|---|---|
| Upload, run with no new documents | 260.0 MB | **5.9 MB** |
| Time to save | 11.5 s | **0.6 s** |

Restore still fetches both halves, because the worker genuinely needs the documents; the saving is
on the upload side, which is the half that can be skipped without changing what the engine can see.

```
Download:  6 runs/day × 260 MB × 30                        = 46.8 GB/month
Upload:    ~1 run/day brings new documents (260 MB)        =  7.8 GB/month
           the other 5 ship 5.9 MB                         =  0.9 GB/month
                                                             ──────────────
                                                             55.5 GB/month × $0.05 = $2.78
```

Against 94 GB and $4.70 before. The residual is dominated by the download, which cannot be skipped
without changing what the engine can see.

## Vercel

The Pro subscription already exists, so the question is only what this workload adds.

| Item | Rate | This workload | Monthly |
|---|---|---|---|
| Pro plan | $20/month | already subscribed | — |
| Function invocations | included at this volume | ~200 dashboard requests + 120 cron checks/day | $0 |
| Active CPU | $0.128/hour | the dashboard is I/O-bound; a cron check is two queries | negligible |
| Blob storage | $0.023/GB-month | ~1.1 GB: dataset snapshots, 226 report files, catalogue | **$0.03** |
| Blob data transfer | $0.05/GB | 55.5 GB, as above | **$2.78** |
| Blob operations | $0.40/1M simple | a few thousand | negligible |

## Neon Postgres

| Item | Free tier | This workload | Monthly |
|---|---|---|---|
| Storage | 0.5 GB | 27 MB, rebuilt not accumulated | $0 |
| Compute | 100 CU-hours/month | scale-to-zero; ~6 worker connections a day plus light browsing | $0 |
| Branches | 10 | 1 | $0 |

Roughly eighteen times the headroom on storage. If it ever outgrows the free tier, Launch is
$0.106/CU-hour and $0.35/GB-month with no monthly minimum.

## GitHub Actions

**$0, because the repository is public.** Standard runners are free and unmetered for public
repositories on every plan.

This is worth stating carefully, because it interacts with the confidentiality question. Since
1 January 2026 there is a $0.002/minute Actions platform charge on top of per-minute runner rates —
and it **does not apply to public repositories**. Making this repository private to remove the
logo from public view would therefore cost:

```
6 runs/day × ~13 min × 30 days             = 2,340 minutes/month
Linux x86 standard runner $0.006/min
plus the platform charge   $0.002/min
                                           = $18.72/month
```

minus the 2,000 minutes a paid plan includes. That is a real number to weigh against the other
options in `production-readiness.md`, not a reason either way.

## Email and monitoring

**$0.** Microsoft Graph sends through an existing mailbox. Delivery is disabled, so it is currently
zero regardless. A transactional provider was considered and rejected: at one recipient and roughly
six messages a month every provider's free tier covers it, and switching would mean re-implementing
a send path that already records three distinct outcomes.

Monitoring is the engine's own `monitor` task plus GitHub's notification on a failed workflow and
Vercel's function logs. No external service is needed at this scale.

## Total

| | Monthly |
|---|---|
| New spend | **about $2.81** — Blob transfer and storage |
| Before the dataset split | about $4.75 |
| Existing Vercel Pro subscription | $20 (unchanged) |

Everything else runs inside a free allowance. No new subscription, upgrade or billable service has
been created or is required.

## Sources

- [Vercel Blob usage and pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing)
- [Vercel pricing](https://vercel.com/docs/pricing)
- [Active CPU pricing for Fluid compute](https://vercel.com/blog/introducing-active-cpu-pricing-for-fluid-compute)
- [Neon pricing](https://neon.com/pricing)
- [GitHub Actions billing](https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions)
- [2026 pricing changes for GitHub Actions](https://github.com/resources/insights/2026-pricing-changes-for-github-actions)
