# End-to-end run: the sixteen checks

Run 2026-09-26T19:58:10+00:00 in the development environment, 18.0 minutes, against a dedicated database (127.0.0.1:55432/azmon_e2e), code at commit a78f2b8 (uncommitted: docs/report-jobs.md only); a full run on a freshly created database, object store and mail directory, every check in one pass. Produced by `python scripts/e2e/run.py`; regenerate this page with `python scripts/e2e/report_md.py`.

**Real in this run:** the reporting engine (fact pack, grounding, python-pptx, LibreOffice PDF), the live CBA website (the deposits table downloaded during each source check), Postgres 16, the production build of the dashboard, Chromium driven by Playwright, the job worker with its leases, heartbeats and fencing, the reaper, the dispatch outbox and the email ledger. J11's dispatch refusal is a real call to the GitHub API.

**Substituted, and why:** private Vercel Blob → a local directory behind the same `ObjectStore` interface; GitHub Actions → the dashboard's local dispatcher, which runs the same `python -m azmonitor.jobs run --job-id` a runner does; Resend → a capture directory. The run uses the `test` environment and the test hooks documented in docs/report-jobs.md, all refused for production jobs. None of the real cloud services were written to.

| # | Check | Kind | Result |
|---|---|---|---|
| J1 | Generate and download the real PDF | local integration (real engine + LibreOffice) | pass |
| J2 | Close the browser and return | local integration (browser) | pass |
| J3 | Double-click without duplicating work | local integration (browser + Postgres) | pass |
| J4 | Reuse an identical edition | local integration (browser + real engine) | pass |
| J5 | A simulated release gets published | local integration (live CBA website + real engine) | pass |
| J6 | One notification is sent | local integration (capture provider) | pass |
| J7 | A repeat check produces no edition or email | local integration (live CBA website) | pass |
| J8 | A correction becomes a revision | local integration (corrected copy of the live CBA file) | pass |
| J9 | A backfill (or translation) raises no alert | local integration (real engine) | pass |
| J10 | A validation failure blocks publication | local integration (real engine, LibreOffice removed) | pass |
| J11 | Recovery: dispatch failure, runner interruption, stale ownership | local integration; the dispatch refusal is a real call to the GitHub API with an invalid token | pass |
| J12 | Email retry | local integration (browser + capture provider) | pass |
| J13 | Unauthorised access is rejected | local integration (HTTP against the production build) | pass |
| J14 | Fresh-runner restore | local integration (empty data directory, local object store) | pass |
| J15 | Overlapping jobs are safe | local integration (two workers, one lease) | pass |
| J16 | Preview cannot email | local integration (preview configuration with a Resend key present) | pass |

## Evidence

### J1. Generate a report in the browser and download the real PDF

```json
{
  "job_id": "job_01m3fkq9renw6z13eqjxc8hjej",
  "edition_id": "monthly:2026-06:v1",
  "pdf_pages": 27,
  "deck_slides": 27,
  "pdf_bytes": 1011744,
  "pdf_first_page": "MONTHLY EDITION\nAzerbaijan Macro &\nBanking Monitor\nEdition 2026-06 \u00b7 banking data to end-Jun 2026 \u00b7\nmacro data to Jan\u2013Aug 2026 \u00b7 CPI Aug 2026\nFacts-only descriptive edition: observed changes in offici",
  "stages": [
    "Queued:done",
    "Collecting (not part of this job):skipped",
    "Calculating:done",
    "Writing narrative:done",
    "Rendering:done",
    "Validating:done",
    "Uploading:done",
    "Complete:done"
  ],
  "cause": "manual_request",
  "requester_email": {
    "sent": 1,
    "subject": "Your report is ready: Monthly Monitor, 2026-06",
    "attachment": {
      "filename": "AZ_Macro_Banking_Monitor_2026-06_v1_20260926T194036Z.pdf",
      "bytes": 1011744
    }
  }
}
```

### J2. Close the browser mid-run and come back to the same job

```json
{
  "stages_when_left": [
    "Queuedwaiting for a runner to start:current",
    "Collecting (not part of this job):skipped",
    "Calculating:pending",
    "Writing narrative:pending",
    "Rendering:pending",
    "Validating:pending",
    "Uploading:pending",
    "Complete:pending"
  ],
  "stages_on_return": [
    "Queued:done",
    "Collecting (not part of this job):skipped",
    "Calculating:done",
    "Writing narrative:done",
    "Renderingevidence workbook:current",
    "Validating:pending",
    "Uploading:pending",
    "Complete:pending"
  ],
  "outcome": "Published"
}
```

### J3. A double click and a second tab start one job

```json
{
  "job_id": "job_01m3fkq9renw6z13eqjxc8hjej",
  "second_request": {
    "job_id": "job_01m3fkq9renw6z13eqjxc8hjej",
    "created": true
  },
  "monthly_jobs_in_database": 1
}
```

### J4. An identical request is offered the existing edition and reuses it

```json
{
  "offer": "An identical validated edition already exists.\nMonthly Monitor 2026-06, version 1, published 2026-09-26 19:41 UTC. Its inputs have not changed since.\nOpen it\nPDF\nPPTX\nXLSX\nEmail it to me\nGenerate anyway\nAn edition with identical inputs is already published. Open it, have it emailed to you, or genera",
  "generate_anyway_job": "job_01m3fkrnewzacs2ktjg32zk1ae",
  "status": "reused",
  "edition_id": "monthly:2026-06:v1",
  "monthly_publications": 1
}
```

### J5. A new official release is detected and a validated edition is published automatically

```json
{
  "source_check": "job_01m3fks4dmk076py174cn6vcvs",
  "status": "succeeded",
  "changes": [
    {
      "classification": "new_observations",
      "dataset_id": "cba_deposits",
      "periods": [
        "2026-07-31",
        "2026-08-31"
      ],
      "handling": "published"
    }
  ],
  "children": [
    {
      "job_id": "job_01m3fkwsx2m8pxb6xsn4hstxgw",
      "report_type": "monthly",
      "status": "succeeded",
      "edition_id": "monthly:2026-07:v40"
    }
  ],
  "publication": [
    {
      "edition_id": "monthly:2026-07:v40",
      "cause": "new_data",
      "version": 40,
      "supersedes": null
    }
  ],
  "waiting": []
}
```

### J6. Exactly one notification is sent to the subscribed owner

```json
{
  "outbox": [
    {
      "delivery_id": "dlv_01m3fkxqfxv24hdvhwb889kby6",
      "purpose": "new_edition",
      "to_address": "owner@e2e.test",
      "status": "accepted",
      "status_reason": null,
      "edition_id": "monthly:2026-07:v40",
      "attempts": 1
    }
  ],
  "captured_subject": "Monthly Monitor: 2026-07",
  "captured_text_head": "Monthly Monitor - 2026-07\nEdition 2026-07, version 40, published 2026-09-26 19:43 UTC\n\nNew official data arrived: cba_deposits for 2026-07-31, 2026-08-31, released 2026-09-25.\n\nKey findings (as validated in the report):\n  - Bank net profit, YTD (AZN mln): 858.7 in Jan\u2013Jul 2026, +175.8 versus Jan\u2013Jul 2025.\n  - Liquid assets / assets (book): 28.4% in end-Jul 2026, -5.6 pp versus end-Jul 2025.\n  - Total deposits, y/y: 11.1% in end-Aug 2026, +4.6 pp versus end-Jul 2026.\n  - FX share of deposits: 34.7% in end-Jul 2026, -3.5 pp versus end-Jul 2025.\n  - Loans to the economy, y/y: 12.9% in end-Jul 2026, +0.8 pp versus end-Jun 2026.\n\nReporting periods:\n  - Consumer prices: 2026-08-31\n  - Macro headline: 2026-08-31\n  - Edition month: 2026-07\n  - Banking data: 2026-07-31\nInformation cutoff: end of 2026-09-26 (Asia/Baku)\n\nOpen the report: http://localhost:3100/reports/monthly/2026-07/40\n  PDF: http:"
}
```

### J7. A repeat check with nothing new produces no edition and no email

```json
{
  "source_check": "job_01m3fkxtez2pf106t8z6eghe7t",
  "status": "unchanged",
  "message": "no relevant change: nothing was published and nobody was emailed"
}
```

### J8. A correction to published figures becomes a revised edition

```json
{
  "cells_changed": [
    {
      "row": 298,
      "column": 1,
      "was": 44501.14899619001,
      "now": 44751.1
    },
    {
      "row": 298,
      "column": 2,
      "was": 17896.660416270002,
      "now": 18146.7
    },
    {
      "row": 298,
      "column": 3,
      "was": 4694.98899651,
      "now": 4945.0
    }
  ],
  "changes": [
    {
      "classification": "substantive_revision",
      "handling": "published"
    }
  ],
  "publications": [
    {
      "edition_id": "monthly:2026-07:v40",
      "version": 40,
      "cause": "new_data",
      "supersedes": null
    },
    {
      "edition_id": "monthly:2026-07:v41",
      "version": 41,
      "cause": "revision",
      "supersedes": "monthly:2026-07:v40"
    }
  ],
  "notice": [
    {
      "delivery_id": "dlv_01m3fm6bbvh2fgbn4xk46e9pqy",
      "purpose": "revised_edition",
      "to_address": "owner@e2e.test",
      "status": "accepted",
      "status_reason": null,
      "edition_id": "monthly:2026-07:v41",
      "attempts": 1
    }
  ]
}
```

### J9. A historical backfill is recorded and raises no alert

```json
{
  "changes": [
    {
      "classification": "historical_backfill",
      "periods": [
        "2019-01-31"
      ],
      "handling": "not_productive"
    }
  ],
  "status": "unchanged",
  "translations": "classified and silenced by the same rule; exercised in tests/test_worker.py (test_translations_and_backfills_are_recorded_and_announce_nothing) with a stand-in engine"
}
```

### J10. A validation failure blocks publication and email

```json
{
  "job": "job_01m3fmaaq762b12phn0d33nbe0",
  "status": "blocked",
  "error_code": "validation_failed",
  "error_message": "Publication was blocked by validation: pdf: no PDF was rendered (skipped)",
  "sector_publications": 0
}
```

### J11. Recovery from dispatch refusal, runner interruption and stale ownership

```json
{
  "dispatch_refused": {
    "job": "job_01m3fmb7jf86ewa1zj8xnz3et4",
    "dispatch_note": "failed: GitHub refused to start the runner (HTTP 401). Check that the dispatch token can run report-job.yml in this repository and that the workflow exists on the main branch.",
    "status": "failed",
    "error_code": "dispatch_rejected",
    "error_message": "GitHub refused to start the runner (HTTP 401). Check that the dispatch token can run report-job.yml in this repository and that the workflow exists on the main branch.",
    "retry": "job_01m3fmb7vx7bvfqvw7yhvs6s99",
    "retry_status": "succeeded"
  },
  "interrupted_and_replaced": {
    "job": "job_01m3fmbsgq5dkgcqhk3jv8aawv",
    "paused_at": {
      "stage": "rendering",
      "attempt": 1,
      "fence": 1
    },
    "reaped": [
      {
        "job_id": "job_01m3fmbsgq5dkgcqhk3jv8aawv",
        "action": "requeued"
      }
    ],
    "requeued": {
      "status": "queued",
      "fence": 2
    },
    "backoff_waited_seconds": 245,
    "final": {
      "status": "succeeded",
      "attempt": 2,
      "fence": 3,
      "edition_id": "sector:trade_2026-07:v1"
    },
    "zombie_exit_code": 4,
    "publications_of_this_edition": [
      {
        "edition_id": "sector:trade_2026-07:v1",
        "job_id": "job_01m3fmbsgq5dkgcqhk3jv8aawv"
      }
    ],
    "events": [
      null,
      "building the fact pack for the trade review",
      "slides",
      null,
      "4 file(s)",
      null,
      "published sector:trade_2026-07:v1",
      "dataset saved"
    ]
  }
}
```

### J12. A failed email is retried on its own from Settings

```json
{
  "delivery": "dlv_01m3fkre2w8wsdv0th2bxe9g77",
  "after_retry": {
    "status": "accepted",
    "attempts": 2,
    "idempotency_key": "dlv_01m3fkre2w8wsdv0th2bxe9g77"
  },
  "ui": {
    "failedRowsBefore": 1,
    "failedRowsAfter": 0
  },
  "note": "provider-level failures, backoff and uncertain sends are covered in web/tests/email-db.test.ts"
}
```

### J13. Unauthorised access is rejected

```json
{
  "status_codes": {
    "api without a session": 401,
    "job page without a session": 401,
    "wrong passphrase": 401,
    "scheduler without its secret": 401,
    "settings without a session": 401,
    "file download without a session": 401,
    "unsigned webhook": 503,
    "forged unsubscribe": 400
  },
  "expected": {
    "api without a session": 401,
    "job page without a session": 401,
    "wrong passphrase": 401,
    "scheduler without its secret": 401,
    "settings without a session": 401,
    "file download without a session": 401,
    "unsigned webhook": 503,
    "forged unsubscribe": 400
  },
  "note": "the webhook answers 503 here because this local run has no webhook secret; with one configured an unsigned call is 401 (web/tests/routes-db.test.ts)"
}
```

### J14. A fresh runner restores the dataset and recognises what is already published

```json
{
  "job": "job_01m3fmnyw8rh4m1x66z24mv960",
  "status": "reused",
  "edition_id": "monthly:2026-07:v41",
  "stages": [
    "queued",
    "starting",
    "starting",
    "waiting_for_dataset",
    "restoring",
    "calculating",
    "calculating",
    "complete"
  ]
}
```

### J15. Two jobs at once take turns on the dataset and both finish

```json
{
  "jobs": {
    "job_01m3fmpaqpxdd2spdaswqc14hb": {
      "status": "succeeded",
      "edition_id": "sector:construction_2026-07:v1"
    },
    "job_01m3fmpaqw120bqaeneb3tn9k9": {
      "status": "succeeded",
      "edition_id": "sector:transport_2026-07:v1"
    }
  },
  "dataset_spans": [
    [
      "2026-09-26T23:57:20.722975+04:00",
      "2026-09-26T23:57:35.894636+04:00"
    ],
    [
      "2026-09-26T23:57:50.731618+04:00",
      "2026-09-26T23:58:06.285209+04:00"
    ]
  ]
}
```

### J16. A preview deployment cannot send email

```json
{
  "ui": {
    "deployment": "Environment\tpreview \u2014 never emails subscribers",
    "result": "Test email: suppressed (provider resend)."
  },
  "ledger": [
    {
      "status": "suppressed",
      "status_reason": "not sent: this is a preview deployment; only production sends email",
      "provider": null
    }
  ],
  "announcements": "publication in a non-production environment records announcements as suppressed (tests/test_worker.py::test_preview_deployments_record_announcements_but_never_queue_them)"
}
```
