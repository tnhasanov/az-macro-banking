"""Turn .e2e/report.json into docs/e2e-report.md, the committed record of the last full run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TITLES = {
    "J1": "Generate and download the real PDF", "J2": "Close the browser and return",
    "J3": "Double-click without duplicating work", "J4": "Reuse an identical edition",
    "J5": "A simulated release gets published", "J6": "One notification is sent",
    "J7": "A repeat check produces no edition or email", "J8": "A correction becomes a revision",
    "J9": "A backfill (or translation) raises no alert", "J10": "A validation failure blocks publication",
    "J11": "Recovery: dispatch failure, runner interruption, stale ownership", "J12": "Email retry",
    "J13": "Unauthorised access is rejected", "J14": "Fresh-runner restore",
    "J15": "Overlapping jobs are safe", "J16": "Preview cannot email",
}


def main() -> int:
    data = json.loads((ROOT / ".e2e" / "report.json").read_text())
    j = data["journeys"]
    lines = [
        "# End-to-end run: the sixteen checks",
        "",
        f"Run {data['ran_at']} on the development machine, {data['minutes']} minutes, against a dedicated "
        f"database ({data['database']}). Produced by `python scripts/e2e/run.py`; regenerate this page with "
        "`python scripts/e2e/report_md.py`.",
        "",
        "**Real in this run:** the reporting engine (fact pack, grounding, python-pptx, LibreOffice PDF), the live CBA "
        "website (the deposits table downloaded during each source check), Postgres 16, the production build of the "
        "dashboard, Chromium driven by Playwright, the job worker with its leases, heartbeats and fencing, the reaper, "
        "the dispatch outbox and the email ledger. J11's dispatch refusal is a real call to the GitHub API.",
        "",
        "**Substituted, and why:** private Vercel Blob → a local directory behind the same `ObjectStore` interface; "
        "GitHub Actions → the dashboard's local dispatcher, which runs the same `python -m azmonitor.jobs run --job-id` "
        "a runner does; Resend → a capture directory. The run uses the `test` environment and the test hooks "
        "documented in docs/report-jobs.md, all refused for production jobs. None of the real cloud services were "
        "written to.",
        "",
        "| # | Check | Kind | Result |",
        "|---|---|---|---|",
    ]
    order = sorted(j, key=lambda k: int(k[1:].split("-")[0]) if k[1:].split("-")[0].isdigit() else 99)
    for key in order:
        v = j[key]
        lines.append(f"| {key} | {TITLES.get(key, v['title'])} | {v['kind']} | {'pass' if v['passed'] else '**FAIL**'} |")
    lines += ["", "## Evidence", ""]
    for key in order:
        v = j[key]
        lines += [f"### {key}. {v['title']}", "", "```json", json.dumps(v["evidence"], indent=2, default=str)[:6000], "```", ""]
    (ROOT / "docs" / "e2e-report.md").write_text("\n".join(lines))
    print(f"wrote docs/e2e-report.md: {sum(v['passed'] for v in j.values())}/{len(j)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
