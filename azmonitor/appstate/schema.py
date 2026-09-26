"""Application state: the durable record of what was asked for, what ran, and what was sent.

Two kinds of table live in the same Postgres database, and they must never be confused:

* **The read model** (`azmonitor/cloud/readmodel.py`) is a projection of the dataset. It is rebuilt
  from the dataset after every run and could be dropped without losing anything.
* **Application state** (this file) is the authority for jobs, publications and notifications.
  Nothing else holds it. A job request, the fact that an edition was published, an email that was
  accepted by the provider — none of these can be rebuilt from anything, so these tables are
  migrated, never recreated, and a migration here is additive only.

The migrations are numbered and recorded in `schema_migrations`. Both the web application and the
worker apply them — whichever reaches the database first — under a transaction-scoped advisory lock,
so two cold starts at once cannot interleave. The same list is exported to TypeScript by
`python -m azmonitor.appstate export-ts`, and a test fails if the two copies drift.

What each constraint is for is written beside it, because the constraints are the design: the
partial unique indexes are what make a double-click, a duplicate cron invocation and a retried send
harmless, at the database, whatever the application code does.
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "jobs, dispatches and events", """
-- A request for work. Created by the web application (a person asked) or by the scheduler tick
-- (a slot came due), executed by a worker on a GitHub Actions runner.
CREATE TABLE IF NOT EXISTS report_jobs (
  job_id            TEXT PRIMARY KEY,
  -- report: produce one report.  source_check: collect, classify, and produce whatever changed.
  -- weekly_digest / monitor: the two other scheduled tasks.
  kind              TEXT NOT NULL CHECK (kind IN ('report','source_check','weekly_digest','monitor')),
  report_type       TEXT,
  -- Validated, normalised parameters. Never a command, a path, a URL or a ref: the worker reads
  -- these and nothing the browser sent reaches a shell.
  params            JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- What makes two requests the same request. See ux_jobs_active_equivalence.
  equivalence_key   TEXT NOT NULL,
  trigger           TEXT NOT NULL CHECK (trigger IN
                      ('manual','schedule','source_change','retry','admin_force')),
  environment       TEXT NOT NULL,
  parent_job_id     TEXT REFERENCES report_jobs(job_id),
  schedule_slot     TEXT,
  requested_by      TEXT NOT NULL,
  -- Where "email me when ready" goes. Resolved from the session when the request is made, never
  -- taken from the request body: a request cannot ask for someone else to be emailed.
  requester_email   TEXT,
  notify_requester  BOOLEAN NOT NULL DEFAULT false,
  force_reason      TEXT,
  status            TEXT NOT NULL CHECK (status IN
                      ('queued','dispatched','running',
                       'succeeded','reused','unchanged','waiting_for_data','blocked','failed','cancelled')),
  stage             TEXT,
  stage_detail      TEXT,
  stage_started_at  TIMESTAMPTZ,
  -- Ownership. `fence` is bumped at every claim and presented on every write the worker makes, so a
  -- worker whose lease lapsed cannot finish, publish or report after another attempt took over.
  attempt           INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 3,
  fence             BIGINT NOT NULL DEFAULT 0,
  worker_id         TEXT,
  lease_expires_at  TIMESTAMPTZ,
  heartbeat_at      TIMESTAMPTZ,
  -- The GitHub Actions run executing the current attempt.
  run_id            BIGINT,
  run_attempt       INTEGER,
  run_url           TEXT,
  requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  next_attempt_at   TIMESTAMPTZ,
  result            JSONB NOT NULL DEFAULT '{}'::jsonb,
  edition_id        TEXT,
  error_code        TEXT,
  error_message     TEXT,
  error_detail      TEXT,
  cancel_requested  BOOLEAN NOT NULL DEFAULT false
);
-- Two equivalent requests cannot both be active. A double-click, or two people asking for the same
-- report at once, loses the insert race and is shown the job that won it. Enforced here rather than
-- by a read-then-write in the application, which is exactly the check a double-click defeats.
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_equivalence
  ON report_jobs (environment, equivalence_key)
  WHERE status IN ('queued','dispatched','running');
-- A scheduled slot produces one job however many times the scheduler fires for it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_schedule_slot
  ON report_jobs (environment, schedule_slot) WHERE schedule_slot IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_jobs_status ON report_jobs (status, requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_parent ON report_jobs (parent_job_id);
CREATE INDEX IF NOT EXISTS ix_jobs_requester ON report_jobs (requested_by, requested_at DESC);

-- What happened to a job, in order. The job row says where it is now; this says how it got there.
CREATE TABLE IF NOT EXISTS job_events (
  id                BIGSERIAL PRIMARY KEY,
  job_id            TEXT NOT NULL REFERENCES report_jobs(job_id),
  at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempt           INTEGER,
  stage             TEXT,
  status            TEXT,
  message           TEXT,
  detail            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_job_events_job ON job_events (job_id, id);

-- The dispatch outbox. Written in the same transaction as the job, so a job can never exist
-- without a record that it still has to be started — the window between "saved" and "sent to
-- GitHub" is where a request is otherwise lost.
CREATE TABLE IF NOT EXISTS job_dispatches (
  dispatch_id       TEXT PRIMARY KEY,
  job_id            TEXT NOT NULL REFERENCES report_jobs(job_id),
  attempt           INTEGER NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('pending','sending','accepted','failed','abandoned')),
  tries             INTEGER NOT NULL DEFAULT 0,
  next_try_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_until     TIMESTAMPTZ,
  accepted_at       TIMESTAMPTZ,
  run_id            BIGINT,
  run_url           TEXT,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_id, attempt)
);
CREATE INDEX IF NOT EXISTS ix_dispatches_due ON job_dispatches (status, next_try_at);
"""),
    (2, "publications, notifications and audit", """
-- The authority for "this edition is published". Written by the worker in one transaction with the
-- job's completion and the notification intents, after every artefact is uploaded and verified.
CREATE TABLE IF NOT EXISTS publication_records (
  edition_id        TEXT PRIMARY KEY,
  report_type       TEXT NOT NULL,
  edition           TEXT NOT NULL,
  version           INTEGER NOT NULL,
  sector            TEXT,
  environment       TEXT NOT NULL,
  job_id            TEXT REFERENCES report_jobs(job_id),
  -- Why it exists: new_data | revision | new_publication | manual_request | admin_force | weekly
  cause             TEXT NOT NULL,
  change_batch_id   TEXT,
  fingerprint       TEXT,
  published_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Every artefact: published key, bytes, sha256, content type, when its upload was verified.
  manifest          JSONB NOT NULL,
  validation        JSONB NOT NULL,
  reporting_periods JSONB NOT NULL DEFAULT '{}'::jsonb,
  information_cutoff TIMESTAMPTZ,
  findings          JSONB NOT NULL DEFAULT '[]'::jsonb,
  limitations       JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- The previous version of the same edition, when this one revises it.
  supersedes        TEXT,
  UNIQUE (report_type, edition, version)
);
CREATE INDEX IF NOT EXISTS ix_publications_type ON publication_records (report_type, published_at DESC);

-- What the engine last computed the fingerprint of a report to be, so the web can offer an
-- identical existing edition immediately instead of starting a job to discover the same thing.
CREATE TABLE IF NOT EXISTS current_fingerprints (
  environment       TEXT NOT NULL,
  report_type       TEXT NOT NULL,
  scope_key         TEXT NOT NULL,          -- period, sector or publication the fingerprint is for
  fingerprint       TEXT NOT NULL,
  edition_id        TEXT,                   -- the published edition with this fingerprint, if any
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (environment, report_type, scope_key)
);

CREATE TABLE IF NOT EXISTS notification_settings (
  environment       TEXT PRIMARY KEY,
  auto_email_enabled BOOLEAN NOT NULL DEFAULT false,
  paused            BOOLEAN NOT NULL DEFAULT false,
  paused_reason     TEXT,
  notify_revisions  BOOLEAN NOT NULL DEFAULT true,
  attach_pdf        BOOLEAN NOT NULL DEFAULT true,
  -- Revised versions of the same edition inside this window are coalesced into one message about
  -- the latest, rather than one message per version.
  revision_settle_minutes INTEGER NOT NULL DEFAULT 120,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by        TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
  recipient_id      TEXT PRIMARY KEY,
  email             TEXT NOT NULL,
  display_name      TEXT,
  role              TEXT NOT NULL DEFAULT 'subscriber' CHECK (role IN ('owner','subscriber')),
  active            BOOLEAN NOT NULL DEFAULT true,
  unsubscribed_at   TIMESTAMPTZ,
  -- Set when the provider reports a permanent failure. A suppressed address is never sent to again
  -- until a person clears it; retrying a hard bounce harms the sender's reputation for everyone.
  suppressed_at     TIMESTAMPTZ,
  suppressed_reason TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recipients_email ON recipients (lower(email));

CREATE TABLE IF NOT EXISTS subscriptions (
  recipient_id      TEXT NOT NULL REFERENCES recipients(recipient_id) ON DELETE CASCADE,
  report_type       TEXT NOT NULL,
  sector            TEXT NOT NULL DEFAULT '*',
  PRIMARY KEY (recipient_id, report_type, sector)
);

-- The delivery ledger and outbox in one table. A row is written in the same transaction as the
-- publication it announces, so a crash cannot publish a report and lose its email.
CREATE TABLE IF NOT EXISTS email_outbox (
  delivery_id       TEXT PRIMARY KEY,
  environment       TEXT NOT NULL,
  edition_id        TEXT REFERENCES publication_records(edition_id),
  job_id            TEXT REFERENCES report_jobs(job_id),
  recipient_id      TEXT REFERENCES recipients(recipient_id),
  to_address        TEXT NOT NULL,
  -- new_edition | revised_edition | manual_request | manual_resend | test | alert
  purpose           TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN
                      ('queued','sending','accepted','delivered','failed','uncertain','suppressed','cancelled')),
  status_reason     TEXT,
  -- Composed at the first attempt and then frozen: every retry sends identical bytes under the same
  -- idempotency key, which is what lets the provider recognise a retry as the same message.
  subject           TEXT,
  body_text         TEXT,
  body_html         TEXT,
  attachment_key    TEXT,
  attachment_name   TEXT,
  attachment_bytes  INTEGER,
  content_hash      TEXT,
  idempotency_key   TEXT NOT NULL,
  attempts          INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 5,
  not_before        TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_until     TIMESTAMPTZ,
  claimed_by        TEXT,
  first_attempt_at  TIMESTAMPTZ,
  last_attempt_ambiguous BOOLEAN NOT NULL DEFAULT false,
  provider          TEXT,
  provider_message_id TEXT,
  accepted_at       TIMESTAMPTZ,
  delivered_at      TIMESTAMPTZ,
  failed_at         TIMESTAMPTZ,
  last_event        TEXT,
  last_event_at     TIMESTAMPTZ,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One announcement of an edition per recipient, ever. This is the guarantee that does not depend on
-- the provider's 24-hour idempotency window: a retry next week still cannot send twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_announcement_once
  ON email_outbox (environment, edition_id, recipient_id)
  WHERE purpose IN ('new_edition','revised_edition');
CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_provider_message
  ON email_outbox (provider, provider_message_id) WHERE provider_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_outbox_due ON email_outbox (status, not_before);
CREATE INDEX IF NOT EXISTS ix_outbox_edition ON email_outbox (edition_id);
CREATE INDEX IF NOT EXISTS ix_outbox_job ON email_outbox (job_id);

-- Provider webhooks, keyed by the provider's own event id so a redelivered event is a no-op.
CREATE TABLE IF NOT EXISTS email_events (
  event_id          TEXT PRIMARY KEY,
  provider          TEXT NOT NULL,
  provider_message_id TEXT,
  event_type        TEXT NOT NULL,
  occurred_at       TIMESTAMPTZ,
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied           BOOLEAN NOT NULL DEFAULT false,
  payload           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_email_events_message ON email_events (provider, provider_message_id);

-- Anything a person decided that the system would not have done on its own.
CREATE TABLE IF NOT EXISTS audit_log (
  id                BIGSERIAL PRIMARY KEY,
  at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  environment       TEXT,
  actor             TEXT NOT NULL,
  action            TEXT NOT NULL,
  target            TEXT,
  reason            TEXT,
  detail            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at DESC);
"""),
    (3, "classified source changes", """
-- What a source check found, classified. The classification decides whether anything is produced
-- and whether anyone is told: a translation, a backfill or a byte-level change with no new data is
-- recorded here and announced nowhere.
CREATE TABLE IF NOT EXISTS source_changes (
  change_id         TEXT PRIMARY KEY,
  batch_id          TEXT NOT NULL,
  environment       TEXT NOT NULL,
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  classification    TEXT NOT NULL CHECK (classification IN
                      ('new_publication','new_observations','substantive_revision',
                       'translation','historical_backfill','bytes_only')),
  source_id         TEXT,
  dataset_id        TEXT,
  publication_id    TEXT,
  document_id       TEXT,
  -- Four different instants, never merged: when the source released it, when a translation
  -- appeared, when this system first saw the document, and when this copy was fetched.
  published_at      DATE,
  translation_available_at DATE,
  first_seen_at     TIMESTAMPTZ,
  retrieved_at      TIMESTAMPTZ,
  language          TEXT,
  periods           JSONB NOT NULL DEFAULT '[]'::jsonb,
  affects           JSONB NOT NULL DEFAULT '[]'::jsonb,
  notifiable        BOOLEAN NOT NULL,
  handled_by_job    TEXT,
  detail            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_changes_batch ON source_changes (batch_id);
CREATE INDEX IF NOT EXISTS ix_changes_detected ON source_changes (environment, detected_at DESC);
"""),

    (4, "state transitions shared by web and worker", """
-- One dispatch in flight per job. Accepted dispatches accumulate as history; an open one (pending or
-- being sent) is unique, so two ticks redriving the outbox cannot start two runs for one attempt.
ALTER TABLE job_dispatches DROP CONSTRAINT IF EXISTS job_dispatches_job_id_attempt_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_open ON job_dispatches (job_id)
  WHERE status IN ('pending','sending');

-- What makes two requests the same request, computed in the database so the web application and
-- the worker cannot disagree about it. jsonb::text is canonical in Postgres (keys in a fixed order,
-- whitespace normalised), which a JSON serialiser in each language would not reliably be.
CREATE OR REPLACE FUNCTION azm_equivalence_key(p_kind TEXT, p_report_type TEXT, p_params JSONB,
                                               p_nonce TEXT DEFAULT NULL) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT md5(concat_ws('|', p_kind, coalesce(p_report_type, ''), p_params::text, coalesce(p_nonce, '')))
$$;

-- Take ownership of one job for one attempt. Returns the claimed row, or nothing when the job is
-- already running under a live lease, finished, cancelled, or out of attempts — which is how a
-- duplicate dispatch of the same job becomes a no-op rather than a second worker.
CREATE OR REPLACE FUNCTION azm_claim_job(p_job TEXT, p_worker TEXT, p_run_id BIGINT,
                                         p_run_attempt INTEGER, p_run_url TEXT,
                                         p_lease_seconds INTEGER DEFAULT 300)
RETURNS SETOF report_jobs LANGUAGE plpgsql AS $$
DECLARE
  claimed report_jobs;
BEGIN
  UPDATE report_jobs j SET
    status = 'running', stage = 'starting', stage_detail = NULL, stage_started_at = now(),
    attempt = j.attempt + 1, fence = j.fence + 1, worker_id = p_worker,
    lease_expires_at = now() + make_interval(secs => p_lease_seconds), heartbeat_at = now(),
    started_at = coalesce(j.started_at, now()), next_attempt_at = NULL,
    run_id = p_run_id, run_attempt = p_run_attempt, run_url = p_run_url,
    error_code = NULL, error_message = NULL, error_detail = NULL
  WHERE j.job_id = p_job
    AND NOT j.cancel_requested
    AND j.attempt < j.max_attempts
    AND (j.status IN ('queued','dispatched')
         OR (j.status = 'running' AND j.lease_expires_at < now()))
  RETURNING j.* INTO claimed;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  -- The dispatch that started this attempt has done its job.
  UPDATE job_dispatches SET status = 'accepted', accepted_at = coalesce(accepted_at, now())
    WHERE job_id = p_job AND status IN ('pending','sending');
  INSERT INTO job_events(job_id, attempt, stage, status, message, detail)
    VALUES (p_job, claimed.attempt, 'starting', 'running', 'claimed by a worker',
            jsonb_build_object('worker', p_worker, 'run_id', p_run_id, 'run_attempt', p_run_attempt));
  RETURN NEXT claimed;
END $$;

-- Extend a lease. False means the lease is gone — another attempt took over, or the job was reaped
-- — and the worker must stop writing.
CREATE OR REPLACE FUNCTION azm_heartbeat(p_job TEXT, p_fence BIGINT, p_lease_seconds INTEGER DEFAULT 300)
RETURNS BOOLEAN LANGUAGE sql AS $$
  WITH u AS (
    UPDATE report_jobs SET heartbeat_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     WHERE job_id = p_job AND fence = p_fence AND status = 'running'
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM u)
$$;

-- Recover from everything that can stop a job without it saying so. Called by the scheduler tick.
--
--  * running, lease lapsed      -> the worker died or hung. Requeue with backoff while attempts
--                                  remain, otherwise fail with a reason. The fence is bumped either
--                                  way, so the lapsed worker can no longer publish if it wakes up.
--  * dispatched, never claimed  -> GitHub accepted the dispatch but no worker ever claimed the job
--                                  (the run failed before starting, or was cancelled). Redispatch a
--                                  bounded number of times, then fail.
--  * queued with nothing open   -> a queued job with no dispatch in flight would wait for ever.
--  * cancel requested, not yet running -> cancelled.
CREATE OR REPLACE FUNCTION azm_reap(p_env TEXT, p_unclaimed_grace_seconds INTEGER DEFAULT 1800,
                                    p_max_redispatch INTEGER DEFAULT 3)
RETURNS TABLE(job_id TEXT, action TEXT) LANGUAGE plpgsql AS $$
DECLARE
  r RECORD;
  n_abandoned INTEGER;
BEGIN
  -- lapsed leases
  FOR r IN SELECT j.* FROM report_jobs j
            WHERE j.environment = p_env AND j.status = 'running' AND j.lease_expires_at < now()
            FOR UPDATE SKIP LOCKED LOOP
    IF r.attempt < r.max_attempts THEN
      UPDATE report_jobs SET status = 'queued', stage = 'queued',
             stage_detail = 'the previous attempt stopped responding; retrying',
             fence = fence + 1, worker_id = NULL, lease_expires_at = NULL,
             next_attempt_at = now() + make_interval(secs => 60 * (4 ^ r.attempt)::int)
       WHERE report_jobs.job_id = r.job_id;
      INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status, next_try_at)
        VALUES ('dsp_' || md5(r.job_id || ':' || r.attempt || ':' || clock_timestamp()::text), r.job_id,
                r.attempt + 1, 'pending', now() + make_interval(secs => 60 * (4 ^ r.attempt)::int))
        ON CONFLICT DO NOTHING;
      INSERT INTO job_events(job_id, attempt, stage, status, message)
        VALUES (r.job_id, r.attempt, r.stage, 'queued',
                'lease lapsed without a heartbeat; requeued for another attempt');
      job_id := r.job_id; action := 'requeued'; RETURN NEXT;
    ELSE
      UPDATE report_jobs SET status = 'failed', fence = fence + 1, worker_id = NULL,
             lease_expires_at = NULL, finished_at = now(), error_code = 'worker_lost',
             error_message = 'The worker stopped responding and no attempts remain.'
       WHERE report_jobs.job_id = r.job_id;
      INSERT INTO job_events(job_id, attempt, stage, status, message)
        VALUES (r.job_id, r.attempt, r.stage, 'failed', 'lease lapsed and attempts are exhausted');
      job_id := r.job_id; action := 'failed'; RETURN NEXT;
    END IF;
  END LOOP;

  -- cancellations that never started
  FOR r IN SELECT j.* FROM report_jobs j
            WHERE j.environment = p_env AND j.cancel_requested AND j.status IN ('queued','dispatched')
            FOR UPDATE SKIP LOCKED LOOP
    UPDATE report_jobs SET status = 'cancelled', finished_at = now(), fence = fence + 1
     WHERE report_jobs.job_id = r.job_id;
    UPDATE job_dispatches SET status = 'abandoned' WHERE job_dispatches.job_id = r.job_id
       AND status IN ('pending','sending');
    INSERT INTO job_events(job_id, attempt, status, message)
      VALUES (r.job_id, r.attempt, 'cancelled', 'cancelled before a worker started it');
    job_id := r.job_id; action := 'cancelled'; RETURN NEXT;
  END LOOP;

  -- dispatched but never claimed
  FOR r IN SELECT j.*, d.dispatch_id AS open_dispatch FROM report_jobs j
             JOIN LATERAL (SELECT * FROM job_dispatches d
                            WHERE d.job_id = j.job_id AND d.status = 'accepted'
                            ORDER BY d.accepted_at DESC LIMIT 1) d ON true
            WHERE j.environment = p_env AND j.status = 'dispatched'
              AND d.attempt > j.attempt
              AND d.accepted_at < now() - make_interval(secs => p_unclaimed_grace_seconds)
            FOR UPDATE OF j SKIP LOCKED LOOP
    UPDATE job_dispatches SET status = 'abandoned',
           last_error = 'accepted by GitHub but no worker claimed the job'
     WHERE dispatch_id = r.open_dispatch;
    SELECT count(*) INTO n_abandoned FROM job_dispatches
     WHERE job_dispatches.job_id = r.job_id AND status = 'abandoned';
    IF n_abandoned < p_max_redispatch THEN
      UPDATE report_jobs SET status = 'queued', stage = 'queued',
             stage_detail = 'the runner never started; dispatching again'
       WHERE report_jobs.job_id = r.job_id;
      INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status)
        VALUES ('dsp_' || md5(r.job_id || ':redispatch:' || clock_timestamp()::text), r.job_id,
                r.attempt + 1, 'pending')
        ON CONFLICT DO NOTHING;
      INSERT INTO job_events(job_id, attempt, status, message)
        VALUES (r.job_id, r.attempt, 'queued', 'no worker claimed the job after dispatch; dispatching again');
      job_id := r.job_id; action := 'redispatched'; RETURN NEXT;
    ELSE
      UPDATE report_jobs SET status = 'failed', finished_at = now(), fence = fence + 1,
             error_code = 'runner_never_started',
             error_message = 'The job was dispatched several times but no runner started it.'
       WHERE report_jobs.job_id = r.job_id;
      INSERT INTO job_events(job_id, attempt, status, message)
        VALUES (r.job_id, r.attempt, 'failed', 'dispatched repeatedly without being claimed');
      job_id := r.job_id; action := 'failed'; RETURN NEXT;
    END IF;
  END LOOP;

  -- queued with no dispatch in flight
  FOR r IN SELECT j.* FROM report_jobs j
            WHERE j.environment = p_env AND j.status = 'queued' AND NOT j.cancel_requested
              AND NOT EXISTS (SELECT 1 FROM job_dispatches d WHERE d.job_id = j.job_id
                                AND d.status IN ('pending','sending'))
            FOR UPDATE SKIP LOCKED LOOP
    INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status, next_try_at)
      VALUES ('dsp_' || md5(r.job_id || ':orphan:' || clock_timestamp()::text), r.job_id,
              r.attempt + 1, 'pending', coalesce(r.next_attempt_at, now()))
      ON CONFLICT DO NOTHING;
    job_id := r.job_id; action := 'dispatch_restored'; RETURN NEXT;
  END LOOP;
END $$;
"""),

    (5, "pending changes, automatic checks and request throttling", """
-- A change is work until a report has answered it. Recording the answer on the change itself makes
-- unhandled changes a durable queue: a check that found new data but could not produce the report
-- (the monthly still waiting for its companion tables, a quality failure, a runner lost mid-render)
-- leaves them pending, and the next check plans from everything still pending rather than only from
-- what it happened to download itself.
ALTER TABLE source_changes ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ;
ALTER TABLE source_changes ADD COLUMN IF NOT EXISTS handling TEXT;
UPDATE source_changes SET handled_at = detected_at, handling = 'published'
 WHERE handled_by_job IS NOT NULL AND handled_at IS NULL;
UPDATE source_changes SET handled_at = detected_at, handling = 'not_productive'
 WHERE handled_at IS NULL
   AND classification NOT IN ('new_publication','new_observations','substantive_revision');
CREATE INDEX IF NOT EXISTS ix_changes_pending ON source_changes (environment, detected_at)
  WHERE handled_at IS NULL;

-- Whether the scheduler may start source checks and digests on its own in this environment. Off
-- until someone turns it on, separately from whether anyone is emailed about what they produce.
ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS auto_checks_enabled BOOLEAN NOT NULL DEFAULT false;

-- The message headers composed with the body (List-Unsubscribe on subscription email), frozen with it.
ALTER TABLE email_outbox ADD COLUMN IF NOT EXISTS headers JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Fixed-window request counters. A generate request starts a runner; a login attempt costs a
-- PBKDF2 derivation; both are worth bounding per signed-in user and per address.
CREATE TABLE IF NOT EXISTS request_throttle (
  bucket            TEXT NOT NULL,
  window_start      TIMESTAMPTZ NOT NULL,
  hits              INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, window_start)
);
"""),
]

LATEST = max(v for v, _, _ in MIGRATIONS)

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_by  TEXT NOT NULL
);
"""

# The advisory-lock key both sides use. Any stable 64-bit number; this one spells "azmstat".
ADVISORY_LOCK_KEY = 0x617A6D73746174
