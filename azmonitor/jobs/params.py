"""What a report request may ask for, checked the same way wherever it arrives.

The web application builds a request from a form; the worker receives only a job id and reads the
request back from the database. Both run it through these rules, so a value the browser could
never have sent — a sector that does not exist, a period in an unexpected shape, a key nobody
defined — is refused before it reaches the engine, however it got into the row.

The rules are deliberately few. A request names a report, a period (or "latest"), a sector for a
sector review, a publication for a brief, and whether to check the sources first. Nothing in it is a
path, a URL, a command, a repository or a branch.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .. import config

REPORT_TYPES: dict[str, dict[str, Any]] = {
    "monthly": {"label": "Monthly Monitor", "period": "month"},
    "weekly": {"label": "Weekly Digest", "period": "week"},
    "sector": {"label": "Sector Review", "period": "month", "sector": True},
    "mpr_brief": {"label": "MPR Brief", "publication_type": "monetary_policy_review"},
    "fsr_brief": {"label": "FSR Brief", "publication_type": "financial_stability_report"},
    "decision_update": {"label": "Policy Decision Update", "publication_type": "policy_decision"},
}
REFRESH = ("latest_data", "check_sources")
ALLOWED_KEYS = {"period", "sector", "publication_id", "refresh"}

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# publication ids are "<type>:<edition>" as the engine assigns them, e.g. policy_decision:2026-07-24
_PUBLICATION = re.compile(r"^[a-z_]{3,40}:[A-Za-z0-9_.\-]{1,60}$")


class InvalidRequest(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sectors() -> list[str]:
    return list((config.reports_config().get("sector") or {}).get("sectors") or {})


def normalise(report_type: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """The canonical parameters for a request, or InvalidRequest saying what is wrong.

    Canonical means the same request always produces the same dictionary, so two people asking
    for the same thing produce the same equivalence key and share one job.
    """
    if report_type not in REPORT_TYPES:
        raise InvalidRequest("unknown_report", f"There is no report type called {report_type!r}.")
    raw = dict(raw or {})
    extra = sorted(set(raw) - ALLOWED_KEYS)
    if extra:
        raise InvalidRequest("unknown_parameter", f"Unexpected parameter(s): {', '.join(extra)}.")
    spec = REPORT_TYPES[report_type]
    out: dict[str, Any] = {}

    refresh = raw.get("refresh") or "latest_data"
    if refresh not in REFRESH:
        raise InvalidRequest("invalid_refresh", "Choose either the latest collected data or a source check first.")
    out["refresh"] = refresh

    if spec.get("publication_type"):
        if raw.get("period") not in (None, "", "latest"):
            raise InvalidRequest("unsupported_period",
                                 f"A {spec['label']} is about one publication; choose the publication, not a period.")
        pub = raw.get("publication_id") or "latest"
        if pub != "latest":
            if not _PUBLICATION.match(str(pub)) or not str(pub).startswith(spec["publication_type"] + ":"):
                raise InvalidRequest("invalid_publication",
                                     f"{pub!r} is not a {spec['label']} source publication.")
        out["publication_id"] = pub
        if raw.get("sector"):
            raise InvalidRequest("unknown_parameter", "A sector applies only to a Sector Review.")
        return out

    period = raw.get("period") or "latest"
    if period != "latest":
        if spec["period"] == "month" and not _MONTH.match(str(period)):
            raise InvalidRequest("invalid_period", "A monthly period is written YYYY-MM, for example 2026-07.")
        if spec["period"] == "week":
            if not _DAY.match(str(period)):
                raise InvalidRequest("invalid_period", "A week is named by its Monday, written YYYY-MM-DD.")
            try:
                monday = dt.date.fromisoformat(str(period))
            except ValueError:
                raise InvalidRequest("invalid_period", f"{period} is not a date.") from None
            if monday.weekday() != 0:
                raise InvalidRequest("invalid_period",
                                     f"{period} is a {monday.strftime('%A')}; a week is named by its Monday.")
    out["period"] = period

    if spec.get("sector"):
        sector = raw.get("sector")
        if not sector:
            raise InvalidRequest("missing_sector", "Choose the sector to review.")
        if sector not in sectors():
            raise InvalidRequest("unknown_sector",
                                 f"There is no configured sector {sector!r}; choose one of {', '.join(sectors())}.")
        out["sector"] = sector
    elif raw.get("sector"):
        raise InvalidRequest("unknown_parameter", "A sector applies only to a Sector Review.")
    if raw.get("publication_id"):
        raise InvalidRequest("unknown_parameter", "A source publication applies only to the briefs.")
    return out


def scope_of(report_type: str, params: dict[str, Any]) -> str:
    """The thing an edition is *of*: a sector, a publication, a week or the latest month."""
    return params.get("sector") or params.get("publication_id") or params.get("period") or "latest"


def describe(report_type: str, params: dict[str, Any]) -> str:
    spec = REPORT_TYPES.get(report_type, {"label": report_type})
    bits = [spec["label"]]
    if params.get("sector"):
        bits.append(params["sector"])
    if params.get("publication_id") and params["publication_id"] != "latest":
        bits.append(params["publication_id"].split(":", 1)[-1])
    if params.get("period") and params["period"] != "latest":
        bits.append(params["period"])
    return " - ".join(bits)
