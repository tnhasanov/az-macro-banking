"""One scheduler, one dispatch target, and a worker that takes nothing but a job id.

These are properties of the repository's configuration rather than of running code, so they are
checked by reading the files: a `schedule:` trigger quietly added back to a workflow, or a
concurrency group added to report-job.yml, would each break the system without failing any test
that runs code.
"""
import json
import re
from pathlib import Path

import yaml

from azmonitor.scheduling.tasks import schedule_drift

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_the_schedule_is_stated_once_and_agrees_with_the_configuration():
    assert schedule_drift() == []


def test_no_workflow_schedules_itself():
    for wf in WORKFLOWS.glob("*.yml"):
        doc = yaml.safe_load(wf.read_text())
        triggers = doc.get("on", doc.get(True)) or {}
        assert "schedule" not in triggers, f"{wf.name} has its own schedule"


def test_vercel_runs_only_the_tick():
    crons = json.loads((ROOT / "web" / "vercel.json").read_text())["crons"]
    assert [c["path"] for c in crons] == ["/api/cron/tick"]
    assert crons[0]["schedule"] == "*/15 * * * *"


def test_report_job_takes_only_a_validated_job_id_and_queues_rather_than_cancels():
    text = (WORKFLOWS / "report-job.yml").read_text()
    doc = yaml.safe_load(text)
    inputs = (doc.get("on", doc.get(True)) or {})["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["job_id"]
    assert "concurrency" not in doc, "a concurrency group cancels pending runs people are waiting on"
    assert re.search(r"\^job_\[0-9a-hjkmnp-tv-z\]\{26\}\$", text), "the job id is checked against its exact shape"
    assert "inputs.job_id" in text and text.count("${{ inputs.") == 1, "no other input reaches the runner"
    assert "python -m azmonitor.jobs run --job-id \"$JOB_ID\"" in text
    assert doc["permissions"] == {"contents": "read"}


def test_the_web_dispatches_only_that_workflow():
    ts = (ROOT / "web" / "lib" / "dispatch.ts").read_text()
    assert 'export const WORKFLOW = "report-job.yml";' in ts
    assert "inputs: { job_id: jobId }" in ts
