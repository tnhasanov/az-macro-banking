"""The whole workflow, from a release appearing to a message being recorded.

Three runs matter, and they are the three this exercises:

1. **A controlled sample release.** A publication appears, readiness passes, a brief is produced and
   a delivery is recorded.
2. **The same run again.** Nothing has changed, so nothing is produced and nothing is sent. This is
   the run that happens three times a day, every day, and getting it wrong means either a flood of
   duplicate reports or a system that stops noticing.
3. **A failed validation.** A critical data-quality failure inside the displayed window blocks the
   edition, the previous one stays current, and an alert says why.

The sources are never contacted. A controlled fixture is the only way to test a release, because a
real one happens when the Central Bank decides and not when a test runs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from azmonitor.delivery.records import DeliveryLedger
from azmonitor.scheduling import readiness as R
from azmonitor.scheduling import tasks as T
from azmonitor.storage.db import Database

TODAY = dt.date(2026, 9, 19)


@pytest.fixture()
def workspace(tmp_data_env):
    """An isolated data and output directory, so nothing touches the real dataset."""
    from azmonitor import config

    config.paths().ensure()
    return tmp_data_env


def _release(db: Database, pub_id: str, pub_type: str, released: str, *, passages: int = 90,
             translated: str | None = None) -> None:
    """A publication as the collector would have stored it, without contacting anything."""
    db.upsert_publication({"publication_id": pub_id, "source_id": "CBA", "pub_type": pub_type,
                           "edition_key": released[:7], "edition_label": released,
                           "published_at": released, "published_at_basis": "date on the publication page",
                           "original_language": "az",
                           **({"translation_available_at": translated} if translated else {})})
    db.replace_passages(f"{pub_id}:doc", [
        {"passage_id": f"{pub_id}#{i}", "doc_id": f"{pub_id}:doc", "publication_id": pub_id, "language": "az",
         "page_index": i // 10, "printed_page": str(i // 10), "section": None, "kind": "text", "ord": i,
         "text": f"Sentence {i} of the publication.", "extraction_method": "pdf_text",
         "validation_status": "verified"} for i in range(passages)])


# ------------------------------------------------------- 1. a controlled release

def test_a_controlled_release_becomes_a_brief_and_a_delivery(workspace, monkeypatch):
    from azmonitor import config
    from azmonitor.delivery import dispatch

    db = Database(config.paths().db_path)
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    rules = (config.schedule_config()["releases"]["mpr_brief"])["readiness"]
    res = R.evaluate_publication(db, "mpr_brief", rules,
                                 dict(db.get_publication("monetary_policy_review:2026-08")), TODAY)
    assert res.state == "ready", res.as_dict()
    assert res.detail["age_days"] == 14        # released a fortnight ago: news, not archive

    # the delivery the run would record, with no provider contacted
    monkeypatch.setenv("AZMONITOR_OWNER_EMAIL", "owner@example.invalid")
    ledger = DeliveryLedger(workspace / "data" / "deliveries.sqlite")
    did, state = ledger.enqueue(report_type="mpr_brief", edition="2026-08", version=1, channel="email",
                                recipient_id="owner", recipient_address="owner@example.invalid",
                                provider="microsoft_graph", subject="CBA MPR 2026-08 - brief",
                                content_hash="h1")
    assert state == "new"
    ledger.mark_sending(did)
    ledger.mark_sent(did, "graph-accepted-202")
    assert ledger.summary() == {"sent": 1}
    ledger.close()
    db.close()


# ------------------------------------------------------------ 2. unchanged rerun

def test_the_same_run_again_produces_nothing_and_sends_nothing(workspace, monkeypatch):
    """The run that happens three times a day. It must be able to do nothing, quietly."""
    from azmonitor import config

    db = Database(config.paths().db_path)
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    # the brief has already been produced and recorded in the run state
    state = {"briefed_publications": ["monetary_policy_review:2026-08"]}
    from azmonitor.scheduling.run_due import _publication_briefs

    out = _publication_briefs(db, {"schedule": {"brief_within_days": 45}}, state, dry_run=False)
    assert out["generated"] == []
    assert "monetary_policy_review:2026-08" in out["already_briefed"]

    # and the delivery ledger refuses a second send of the same edition to the same person
    ledger = DeliveryLedger(workspace / "data" / "deliveries.sqlite")
    kw = dict(report_type="mpr_brief", edition="2026-08", version=1, channel="email",
              recipient_id="owner", recipient_address="owner@example.invalid",
              provider="microsoft_graph", subject="s", content_hash="h1")
    did, _ = ledger.enqueue(**kw)
    ledger.mark_sending(did)
    ledger.mark_sent(did, "m1")
    assert ledger.enqueue(**kw)[1] == "sent"
    assert ledger.summary() == {"sent": 1}       # still one, not two
    ledger.close()
    db.close()


# --------------------------------------------------------- 3. failed validation

def test_a_failed_validation_blocks_the_edition_and_explains_itself(workspace):
    """A critical failure inside the displayed window stops publication. The previous edition stays
    current, which is the point: a board pack with a known-wrong figure is worse than a late one."""
    from azmonitor import config

    rules = config.schedule_config()["releases"]["monthly"]["readiness"]
    gate = R.quality_gate("monthly", rules, {"summary": {"critical": 1, "warning": 0}})

    assert gate is not None and gate.state == "blocked" and not gate.ok
    assert "previous edition stays current" in gate.detail["note"]
    failed_rule = next(r for r in gate.reasons if r["rule"] == "require_quality_pass")
    assert failed_rule["met"] is False and "1 critical" in failed_rule["saw"]

    # the same failures outside the window are warnings and stop nothing
    assert R.quality_gate("monthly", rules, {"summary": {"critical": 0, "warning": 5}}) is None


def test_a_backfill_of_the_archive_triggers_nothing(workspace):
    """The run after a backfill. Ninety publications arrive at once and none of them is news."""
    from azmonitor import config
    from azmonitor.scheduling.run_due import _publication_briefs

    db = Database(config.paths().db_path)
    for year in range(2020, 2026):
        _release(db, f"monetary_policy_review:{year}-03", "monetary_policy_review", f"{year}-04-15")
    # plus one that really is recent
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    state: dict = {}
    out = _publication_briefs(db, {"schedule": {"brief_within_days": 45}}, state, dry_run=True)
    would = [g["publication_id"] for g in out["generated"]]

    assert would == ["monetary_policy_review:2026-08"]
    assert len(out["skipped_old"]) == 6
    db.close()


# ------------------------------------------------------------- the weekly window

def test_the_weekly_digest_covers_the_week_that_just_ended(workspace):
    monday = dt.datetime(2026, 9, 21, 8, 30, tzinfo=T.tz())
    start, end = T.previous_calendar_week(monday)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-14", "2026-09-20")
    assert start.weekday() == 0 and end.weekday() == 6

    # the boundaries do not move with the day the digest is actually produced
    wednesday = dt.datetime(2026, 9, 23, 17, 0, tzinfo=T.tz())
    assert T.previous_calendar_week(wednesday) == (start, end)

    lo, hi = T.week_bounds_utc(start, end)
    assert lo.startswith("2026-09-13T20:00")        # Baku is UTC+4, so the week starts the evening before
    assert hi.startswith("2026-09-20T19:59")


def test_a_missed_run_is_only_missed_once_something_has_ever_run(workspace):
    """A fresh install has not missed anything; a host that was off for a day has missed several."""
    assert T.missed_runs() == []                     # no history at all

    hist = {"runs": [{"task": "source-check", "status": "ok",
                      "at_utc": "2026-09-14T05:15:00+00:00", "at_local": "2026-09-14T09:15:00+04:00"}]}
    (workspace / "data" / "state").mkdir(parents=True, exist_ok=True)
    (workspace / "data" / "state" / "run_history.json").write_text(json.dumps(hist), encoding="utf-8")

    now = dt.datetime(2026, 9, 15, 20, 0, tzinfo=T.tz())
    missed = T.missed_runs(now)
    # the 13:15 and 17:15 on the 14th, and all three on the 15th
    assert [m["due_local"][5:16] for m in missed] == ["09-14T13:15", "09-14T17:15",
                                                      "09-15T09:15", "09-15T13:15", "09-15T17:15"]
    assert all(m["task"] == "source-check" for m in missed)
    assert all(m["due_local"].endswith("+04:00") for m in missed)   # reported in Asia/Baku


# --------------------------------------------------------------- report archive

def test_retention_keeps_the_latest_and_anything_delivered(workspace):
    """A recipient holding a PDF and finding the archive has forgotten it is worse than a big disk."""
    from azmonitor import config
    from azmonitor.delivery.records import DeliveryLedger
    from azmonitor.scheduling import archive

    out = config.paths().output_dir
    edition = out / "monthly" / "2026-07"
    # v9 sorts after v36 as a string: the ordering this exercises is numeric
    for n in (1, 2, 9, 12, 36):
        d = edition / f"v{n}_20260919T00000{n}Z"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"version": n}), encoding="utf-8")
        (d / "deck.pdf").write_bytes(b"x" * 1024)
    (out / "latest").mkdir(parents=True, exist_ok=True)
    (out / "latest" / "monthly").symlink_to(edition / "v36_20260919T000036Z")

    assert archive._version_number(edition / "v36_x") == 36
    assert [archive._version_number(p) for p in archive._versions(edition)] == [1, 2, 9, 12, 36]

    # v2 was delivered to someone and must survive a retention limit of two
    led = DeliveryLedger(config.paths().data_dir / "deliveries.sqlite")
    did, _ = led.enqueue(report_type="monthly", edition="2026-07", version=2, channel="email",
                         recipient_id="owner", recipient_address="o@example.invalid",
                         provider="microsoft_graph", subject="s", content_hash="h")
    led.mark_sending(did)
    led.mark_sent(did, "m1")
    led.close()

    import azmonitor.config as C
    original = C.schedule_config
    C.schedule_config = lambda: {**original(), "archive": {"keep_versions_per_edition": 2, "keep_editions": {}}}
    try:
        p = archive.plan()
        removed = {r["version"] for r in p["would_remove"]}
        kept = {k["reason"] for k in p["kept_despite_limits"]}
        assert removed == {1, 9}                       # v2 delivered, v12 and v36 within the limit
        assert "this version was delivered to a recipient" in kept
        assert p["applied"] is False if "applied" in p else True
    finally:
        C.schedule_config = original


def test_a_late_weekly_digest_still_covers_only_its_own_week(workspace):
    """A digest produced on Wednesday must not report Monday's and Tuesday's releases under last
    week's heading. The window closes at the Sunday, whenever the digest is actually produced."""
    from azmonitor import config
    from azmonitor.storage.db import Database

    db = Database(config.paths().db_path)
    inside = "2026-09-18T10:00:00+00:00"       # Friday of the window
    outside = "2026-09-22T10:00:00+00:00"      # the following Tuesday
    for doc_id, seen in (("in", inside), ("out", outside)):
        db.upsert_document({"doc_id": doc_id, "source_id": "CBA", "dataset_id": "cba_deposits",
                            "document_url": f"https://example.invalid/{doc_id}", "sha256": doc_id,
                            "retrieved_at": seen, "first_seen_at": seen, "published_at": seen[:10],
                            "status": "parsed"})

    start, end = dt.date(2026, 9, 14), dt.date(2026, 9, 20)
    end_stamp = end.isoformat() + "T23:59:59+99:99"
    in_window = [d["doc_id"] for d in db.all_documents()
                 if (d["first_seen_at"] or "") >= start.isoformat()
                 and (d["first_seen_at"] or "") <= end_stamp]

    assert in_window == ["in"]
    db.close()


# ------------------------------------------------- the schedule, written three times

def test_the_baku_to_utc_conversion_holds_all_year(workspace):
    """GitHub cron is UTC only, so the workflow holds converted times. Asia/Baku has had no
    daylight saving since 2016, but that is a fact about the world rather than an assumption worth
    making, so it is checked against the timezone database in both winter and summer."""
    from zoneinfo import ZoneInfo

    from azmonitor.scheduling.tasks import BAKU_UTC_OFFSET_HOURS

    baku = ZoneInfo("Asia/Baku")
    for month in (1, 4, 7, 10):
        for hour in (3, 9, 15, 21):
            utc = dt.datetime(2026, month, 15, hour, 15, tzinfo=dt.timezone.utc)
            offset = utc.astimezone(baku).utcoffset().total_seconds() / 3600
            assert offset == BAKU_UTC_OFFSET_HOURS, (
                f"Asia/Baku is UTC+{offset} on {utc:%d %b}, not the UTC+{BAKU_UTC_OFFSET_HOURS} the "
                f"workflow crons assume")


def test_the_three_copies_of_the_schedule_agree(workspace):
    """config/schedule.yaml, the systemd timers and the GitHub crons. A report that silently stops
    arriving is how this drift is otherwise discovered."""
    from azmonitor.scheduling.tasks import schedule_drift, workflow_drift

    assert workflow_drift() == []
    assert schedule_drift() == []


# ------------------------------------------------- how a run gets a Blob credential

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """The workflow with its comments removed.

    A test about what a workflow *does* must not match the prose explaining why it stopped doing
    something else — the comment saying `vercel env pull` cannot work here contains the very string
    that would prove it still ran.
    """
    return "\n".join(l for l in _workflow(name).split("\n") if not l.strip().startswith("#"))


@pytest.mark.parametrize("name", ["manual-run.yml", "scheduled.yml"])
def test_a_run_mints_its_own_blob_credential(name):
    """The read-write token cannot be a GitHub secret, because it cannot be read.

    Opting it into a project connection stores it as a *sensitive* Vercel variable, which is
    write-only by design: no reveal, no API, and `vercel env pull` returns it empty. So each run
    mints a short-lived OIDC token instead, and the step that does it must be there in both
    workflows or the scheduled one fails at the point of saving while the manual one works.
    """
    body = _workflow(name)
    assert "Mint a short-lived Blob credential" in body
    assert "node tools/blob/vercel-oidc.mjs" in body
    assert "BLOB_STORE_ID: ${{ secrets.BLOB_STORE_ID }}" in body, \
        "an OIDC token names no store by itself"


@pytest.mark.parametrize("name", ["manual-run.yml", "scheduled.yml"])
def test_the_run_does_not_reach_for_env_pull(name):
    """`vercel env pull` cannot work with a project-scoped access token, and failed as a linking
    error rather than an authorisation one.

    The CLI fetches the team alongside the project — `getOrgById` settled in parallel with the
    project lookup — and a project-scoped token is denied team-level resources by design. The 403
    surfaces as "Could not retrieve Project Settings ... remove the `.vercel` directory", which
    sends you looking for a link that was never missing. Minting through the project's own token
    endpoint asks for nothing at team level.
    """
    code = _code(name)
    assert "env pull" not in code
    assert "VERCEL_CLI_VERSION" not in code, "no CLI is invoked any more, so none is pinned"


@pytest.mark.parametrize("name", ["manual-run.yml", "scheduled.yml"])
def test_minting_is_skipped_when_a_read_write_token_exists(name):
    """If a readable store-scoped token ever becomes available it is the better credential, and
    configuring it must not require editing a workflow."""
    body = _workflow(name)
    assert "if: ${{ !env.BLOB_READ_WRITE_TOKEN && env.VERCEL_TOKEN }}" in body


@pytest.mark.parametrize("name", ["manual-run.yml", "scheduled.yml"])
def test_minting_writes_no_file_at_all(name):
    """`env pull` wrote every non-sensitive variable in the environment to disk, the database URL
    among them, and the step had to be careful to delete it. Asking only for the token writes
    nothing, so there is nothing to leak or to clean up."""
    body = _workflow(name)
    step = body[body.index("Mint a short-lived Blob credential"):]
    step = step[step.index("run:"):]
    step = step[:step.index("- name:")]
    assert ".env" not in step and "cat " not in step


@pytest.mark.parametrize("name", ["manual-run.yml", "scheduled.yml"])
def test_the_credential_is_proved_before_the_lease_is_taken(name):
    """Order matters: a run that takes the lease and then fails to authenticate has blocked the
    next one for the length of the lease for nothing."""
    body = _workflow(name)
    assert body.index("Mint a short-lived Blob credential") \
        < body.index("Prove the Blob credential before anything writes") \
        < body.index("Take the run lease")


# ------------------------------------- the workflow file that ran vs the branch it ran against

def _dispatch_guard_script() -> str:
    """The `run:` body of the revision check, as bash, with the one Actions expression stubbed."""
    import yaml

    steps = yaml.safe_load(_workflow("manual-run.yml"))["jobs"]["run"]["steps"]
    step = next(s for s in steps if s.get("name", "").startswith("The workflow that ran"))
    return step["run"].replace("${{ inputs.ref }}", "the-branch")


def _run_guard(tmp_path, *, branch_revision: str | None, running_revision: str):
    """Run the guard for real, against a checkout whose workflow declares `branch_revision`."""
    import subprocess

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    line = f'      WORKFLOW_REVISION: "{branch_revision}"\n' if branch_revision else ""
    (workflows / "manual-run.yml").write_text(f"env:\n      TZ: Asia/Baku\n{line}", encoding="utf-8")
    return subprocess.run(["bash", "-c", _dispatch_guard_script()], cwd=tmp_path,
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "WORKFLOW_REVISION": running_revision})


def test_the_running_workflow_matching_the_branch_is_allowed(tmp_path):
    result = _run_guard(tmp_path, branch_revision="1", running_revision="1")
    assert result.returncode == 0, result.stderr
    assert "matches" in result.stdout


def test_an_older_workflow_file_stops_the_run_and_names_the_fix(tmp_path):
    """The failure this exists for: `workflow_dispatch` runs the file from the branch chosen in the
    dropdown while the engine comes from the `ref` input, so dispatching from a default branch
    holding an older copy ran an older workflow against newer code — green, and having skipped the
    steps that authenticate."""
    result = _run_guard(tmp_path, branch_revision="2", running_revision="1")
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "revision 1" in result.stdout and "revision 2" in result.stdout
    assert "Use workflow from" in result.stdout, "the message has to say how to fix it"


def test_a_branch_with_no_revision_marker_is_refused(tmp_path):
    result = _run_guard(tmp_path, branch_revision=None, running_revision="1")
    assert result.returncode == 1
    assert "no WORKFLOW_REVISION" in result.stdout


def test_the_check_runs_before_anything_expensive(tmp_path):
    """Two seconds of checkout, not forty minutes of run, before the mismatch is reported."""
    import yaml

    names = [s.get("name") or s.get("uses") for s in
             yaml.safe_load(_workflow("manual-run.yml"))["jobs"]["run"]["steps"]]
    assert names[0] == "actions/checkout@v4", "the guard needs the checkout to compare against"
    assert names[1].startswith("The workflow that ran"), "and nothing should precede it after that"
