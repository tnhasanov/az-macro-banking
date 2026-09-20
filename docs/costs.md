# What this costs to run

Estimated from the measured workload, at the pricing in effect in September 2026. Every figure
below traces to something measured in this repository rather than to a guess about usage.

**Nothing in this plan requires a new paid resource.** The one line that could become chargeable is
Blob data transfer, and it is the one worth watching; the arithmetic is below.

## The workload, measured

| Quantity | Measured value | Where it comes from |
|---|---|---|
| Runs per day | 6 | three source checks, two monitors, one weekly digest on Mondays |
| Dataset size on disk | 379 MB | `data/` — SQLite 34 MB, raw source documents 280 MB, state and analytics |
| Dataset tarball | 260 MB | `dataset/monitor-*.tar.gz`, gzip |
| Report archive | 419 MB over 101 editions | `outputs/` — PDF 51 MB, PPTX 23 MB, workbooks 105 MB |
| Mean PDF | 0.51 MB | 101 files |
| Read model in Postgres | 27 MB | 57,974 indicator rows plus publications, editions, runs, quality |
| Dashboard page views | a handful a day | one reader |

## Vercel

The Pro subscription already exists, so the question is only whether this workload adds to it.

| Item | Rate | This workload | Monthly |
|---|---|---|---|
| Pro plan | $20/month | already subscribed | — |
| Function invocations | included at this volume | ~200 dashboard requests + 120 cron checks/day | $0 |
| Active CPU | $0.128/hour | the dashboard is I/O-bound; cron checks are two queries | negligible |
| Blob storage | $0.023/GB-month | ~1.1 GB (dataset + archive, with retention) | **$0.03** |
| Blob data transfer | $0.05/GB | see below | **$4.70** |
| Blob simple operations | $0.40/1M | a few thousand | negligible |

### Blob transfer is the line that matters

The engine restores the dataset at the start of every cycle and saves it at the end, inside its own
lock. At 260 MB each way, six times a day:

```
6 runs/day × (260 MB down + 260 MB up) × 30 days ≈ 94 GB/month × $0.05 = $4.70/month
```

That is affordable but it is almost entirely waste: **roughly 280 MB of the 379 MB dataset is the
`raw/` directory of downloaded source documents, which is append-mostly.** A monitor run that
changes nothing still ships all of it twice.

**Recommended optimisation, not yet implemented:** split the dataset into two objects — the
databases and state (about 70 MB, changes on every run) and the raw document archive (280 MB,
changes only when a source publishes) — and re-upload the raw part only when its manifest changes.
That would cut transfer to roughly 12 GB/month, about $0.60, and take one to two minutes off every
run. It is left as a follow-up rather than rushed in at the end of this work, because it changes
the storage layout and cannot be tested against real Vercel Blob from here.

## Neon Postgres

| Item | Free tier | This workload | Monthly |
|---|---|---|---|
| Storage | 0.5 GB | 27 MB, rebuilt not accumulated | $0 |
| Compute | 100 CU-hours/month | scale-to-zero; ~6 worker connections/day plus light browsing | $0 |
| Branches | 10 | 1 | $0 |

Comfortably inside the free tier, with roughly eighteen times the headroom on storage. If it ever
outgrows it, the Launch plan is $0.106/CU-hour and $0.35/GB-month with no monthly minimum.

## GitHub Actions

**$0.** This repository is public, and GitHub Actions minutes are free and unmetered for public
repositories. At six runs a day of about twelve minutes, a private repository would use roughly
2,200 minutes a month against the 2,000 free on a paid plan — so making the repository private
would introduce a real, if small, bill. Worth knowing before changing its visibility.

## Email

Microsoft Graph sends through an existing mailbox at no additional cost. Delivery is disabled, so
this is currently $0 regardless. A transactional provider was considered and rejected: at one
recipient and roughly six messages a month, every provider's free tier covers it, and switching
would mean re-implementing and re-testing a send path that already works and already records three
distinct outcomes.

## Monitoring

**$0.** The engine's own `monitor` task checks for stale sources and missed runs and is the primary
alerting path. GitHub notifies on a failed workflow run. Vercel's built-in function logs cover the
dashboard. No external monitoring service is needed at this scale.

## Total

| | Monthly |
|---|---|
| New spend, as built | **about $4.75** — Blob transfer and storage |
| New spend, with the transfer optimisation | **about $0.65** |
| Existing Vercel Pro subscription | $20 (unchanged) |

Everything else runs inside a free allowance. No new subscription, upgrade or billable service has
been created or is required.
