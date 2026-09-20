"""What a failed cycle is allowed to do.

The workflow captures `run-task`'s exit code instead of letting it end the job, because the lease
still has to be released and the failure still has to be recorded. The danger in that shape is that
the steps which follow stop asking whether the run actually worked.

One outcome makes this severe rather than untidy. `failed_restore` (5) means the dataset was never
pulled out of the store, so the SQLite database on disk is empty. Publishing the read model from
it would replace every figure on the dashboard with nothing — a storage failure turned into data
loss, reported as a successful-looking dashboard with no numbers on it.

These tests read the workflow itself, because the gating lives there and a test that re-implemented
it would pass while the workflow stayed wrong.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WORKFLOW = pathlib.Path(".github/workflows/scheduled.yml")


@pytest.fixture(scope="module")
def steps() -> dict[str, dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {s.get("name", s.get("uses", "")): s for s in doc["jobs"]["run"]["steps"]}


@pytest.fixture(scope="module")
def run_step(steps) -> str:
    return steps["Run the task"]["run"]


# The engine's own mapping, from azmonitor/cli.py. Kept here so a change to it fails a test rather
# than silently changing what the workflow publishes.
EXIT_CODES = {
    "ok": 0, "partial": 2, "failed": 3, "skipped_locked": 4,
    "failed_restore": 5, "failed_save": 6,
}
MAY_PUBLISH = {"ok", "partial"}


def test_the_exit_codes_are_still_what_the_workflow_assumes():
    from azmonitor import cli

    source = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    mapping = re.search(r'return \{"ok": 0.*?\}\.get', source, re.S)
    assert mapping, "cmd_run_task no longer maps statuses to exit codes in the expected shape"
    for status, code in EXIT_CODES.items():
        assert f'"{status}": {code}' in mapping.group(0), f"{status} is no longer {code}"


def _publish_decision(run_step: str, code: int) -> str:
    """Read the case statement the workflow uses, rather than guessing what it does."""
    block = re.search(r"case \"\$code\" in\s*(.*?)esac", run_step, re.S)
    assert block, "the publish decision is no longer a case statement on the exit code"
    body = block.group(1)
    for line in body.strip().splitlines():
        pattern, _, action = line.partition(")")
        codes = [p.strip() for p in pattern.strip().split("|")]
        if str(code) in codes or "*" in codes:
            return "yes" if "publish=yes" in action else "no"
    raise AssertionError(f"exit code {code} is not covered by the publish decision")


@pytest.mark.parametrize("status,code", sorted(EXIT_CODES.items(), key=lambda kv: kv[1]))
def test_only_a_complete_saved_dataset_may_publish(run_step, status, code):
    expected = "yes" if status in MAY_PUBLISH else "no"
    assert _publish_decision(run_step, code) == expected, (
        f"{status} (exit {code}) must {'' if expected == 'yes' else 'not '}publish")


def test_a_failed_restore_never_publishes(run_step):
    """Stated on its own, because it is the one that loses data rather than a report."""
    assert _publish_decision(run_step, 5) == "no"


def test_an_unrecognised_exit_code_does_not_publish(run_step):
    """A new failure mode must be treated as a failure until someone decides otherwise."""
    assert _publish_decision(run_step, 99) == "no"


def test_publication_is_gated_on_the_task_not_on_the_lease(steps):
    """Holding the lease only means this worker was allowed to try."""
    condition = steps["Publish what the dashboard reads"]["if"]
    assert "steps.run.outputs.publish == 'yes'" in condition
    assert "!inputs.dry_run" in condition


def test_a_run_that_did_not_publish_is_still_recorded(steps):
    step = steps["Record a run that did not publish"]
    assert "always()" in step["if"]
    assert "steps.run.outputs.publish != 'yes'" in step["if"]
    assert "--status" in step["run"], "the outcome must reach the run history"
    assert "--error" in step["run"]


def test_a_successful_run_is_recorded_as_published(steps):
    assert "--published" in steps["Publish what the dashboard reads"]["run"]


def test_the_catalogue_is_verified_after_publishing(steps):
    """Every edition the dashboard offers must reference a file that is really in the store."""
    assert "publish verify" in steps["Publish what the dashboard reads"]["run"]


def test_the_lease_is_released_whatever_happened(steps):
    step = steps["Release the lease"]
    assert step["if"].startswith("always()")
    assert "AZMONITOR_LEASE_HOLDER" in step["run"], "release must name the holder that took it"


@pytest.mark.parametrize("code,should_fail", [
    (0, False), (2, False), (4, False), (3, True), (5, True), (6, True), (99, True),
])
def test_the_job_goes_red_for_real_failures_only(steps, code, should_fail):
    gate = steps["Fail the job if the run failed"]["run"]
    block = re.search(r"case \"\$\{\{ steps\.run\.outputs\.exit_code \}\}\" in\s*(.*?)esac",
                      gate, re.S)
    assert block, "the failure gate is no longer a case statement"
    body = block.group(1)
    matched = None
    for chunk in re.finditer(r'([0-9|"]+)\)(.*?);;', body, re.S):
        codes = [c.strip().strip('"') for c in chunk.group(1).split("|")]
        if str(code) in codes:
            matched = chunk.group(2)
            break
    if matched is None:
        matched = re.search(r"\*\)(.*?);;", body, re.S).group(1)
    exits_nonzero = "exit 1" in matched
    assert exits_nonzero == should_fail, f"exit {code}: expected job failure={should_fail}"


def test_lock_contention_is_not_a_job_failure(steps):
    gate = steps["Fail the job if the run failed"]["run"]
    assert "another run holds the lease" in gate
    # And an empty exit code — the task step never completed — is a failure, not a pass.
    assert '""' in gate and "did not run to completion" in gate


def test_the_summary_says_whether_the_dashboard_was_updated(steps):
    summary = steps["Run summary"]["run"]
    for outcome in EXIT_CODES:
        assert outcome in summary, f"the summary does not mention {outcome}"
    assert "The dashboard was left alone" in summary
    assert "last good dataset is still in the store" in summary


def test_the_worker_carries_its_fence_token_forward(steps):
    """Every later step has to be able to prove it is still the worker that took the lease."""
    lease = steps["Take the run lease"]["run"]
    assert "AZMONITOR_LEASE_HOLDER" in lease and "AZMONITOR_LEASE_FENCE" in lease
    assert "GITHUB_ENV" in lease, "the fence must reach later steps, not just this one"
