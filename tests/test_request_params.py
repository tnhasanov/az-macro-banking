"""The request rules, on the cases the web application's tests use too.

tests/fixtures/request-cases.json is read here and by web/tests/params.test.ts. If the two
implementations ever disagree about a case, one of these tests fails.
"""
import json
from pathlib import Path

import pytest

from azmonitor.jobs import params as PR

CASES = json.loads((Path(__file__).parent / "fixtures" / "request-cases.json").read_text())


def test_the_fixture_names_the_configured_sectors():
    assert sorted(CASES["sectors"]) == sorted(PR.sectors())


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: f"{c['report']}:{json.dumps(c['raw'])}")
def test_request_rules(case):
    if "ok" in case:
        assert PR.normalise(case["report"], case["raw"]) == case["ok"]
    else:
        with pytest.raises(PR.InvalidRequest) as exc:
            PR.normalise(case["report"], case["raw"])
        assert exc.value.code == case["error"]


def test_the_web_application_lists_the_configured_sectors():
    import re

    ts = (Path(__file__).parents[1] / "web" / "lib" / "params.ts").read_text()
    listed = re.search(r"export const SECTORS = \[([^\]]*)\]", ts).group(1)
    assert sorted(re.findall(r'"([a-z_]+)"', listed)) == sorted(PR.sectors())
