"""`python -m azmonitor.jobs run --job-id job_...`

The only command a dispatched runner executes. It takes a job id — validated against its exact
shape — and nothing else: what to produce is read from the job row.

Exit codes: 0 when the job reached any recorded outcome (a blocked or failed report is a recorded
outcome, not a crashed runner), 2 for a malformed invocation, 3 when the job could not be claimed,
1 when the worker itself broke before it could record anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from ..appstate import JOB_ID_PATTERN


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m azmonitor.jobs")
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="claim and run one job")
    r.add_argument("--job-id", required=True)
    args = ap.parse_args(argv)

    if not re.match(JOB_ID_PATTERN, args.job_id):
        print(json.dumps({"error": "not a job id"}))
        return 2

    from .. import config
    from ..cloud import readmodel as RM
    from ..util.log import setup_logging
    from .worker import Worker

    setup_logging(config.paths().logs_dir)
    run_id = os.environ.get("GITHUB_RUN_ID")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if run_id and repo else None
    worker = Worker(connect=RM.connect, run_id=int(run_id) if run_id else None,
                    run_attempt=int(run_attempt) if run_attempt else None, run_url=run_url)
    result = worker.run(args.job_id)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("claimed") else 3


if __name__ == "__main__":
    sys.exit(main())
