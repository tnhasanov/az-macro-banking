"""Claim-level grounding: binding every number in the narrative to one fact.

A number that merely exists somewhere in the fact pack proves nothing. "Deposits grew 6.5%" is
wrong if 6.5 is the loan growth of a different month, and the old check — does this value appear in
the approved list — passes it. So each number a narrative writes has to be bound to a claim that
names the metric, the dimension slice, the period, the unit and the comparison basis, and the claim
is then re-resolved against the fact pack and checked on all of them, including the direction the
sentence asserts.

Claim reference format (`claim_id`):

    <metric_ref>[|<dims json>]@<period>#<basis>

for example

    cba.deposits.total.yoy@2026-07-31#yoy
    cba.rates.new.loan|{"currency": "AZN"}@2026-07-31#level
    cba.fsr.car@2025-12-31#level

`basis` is one of level, yoy, mom, ytd, change, pp_change, bp_change, share, contribution, forecast,
stress. It has to agree with what the fact pack says the metric is, so a year-on-year rate cannot be
presented as a level, and a percentage-point move cannot be written as a percentage.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# A number as written in the text, with the unit marker that follows it.
NUMBER_TOKEN = re.compile(
    # the grouped form must actually carry a separator, otherwise "2026" matches as "202"
    r"(?<![\w.])(?P<sign>[+−-])?(?P<num>\d{1,3}(?:[,\u00a0 ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])"
    r"\s*(?P<unit>%|pp|p\.p\.|bp|bps|basis points|percentage points?|mln|bn|billion|million|times|x)?",
    re.IGNORECASE)

UNIT_ALIASES = {
    "%": "%", "pp": "pp", "p.p.": "pp", "percentage point": "pp", "percentage points": "pp",
    "bp": "bp", "bps": "bp", "basis points": "bp",
    "mln": "mln", "million": "mln", "bn": "bn", "billion": "bn", "times": "x", "x": "x",
}
BASES = {"level", "yoy", "mom", "ytd", "change", "pp_change", "bp_change", "share", "contribution",
         "forecast", "stress", "ratio", "count"}
# how a metric's own unit and the reference suffix map onto an allowed claim basis
SUFFIX_BASIS = {"": "level", ".prior": "level", ".change": "change"}

# Matched on word boundaries: "flat" must not match inside "inflation".
UP_WORDS = ("up", "rose", "rises", "risen", "increase", "increased", "increases", "higher", "grew", "grows",
            "added", "gained", "widened", "accelerated", "climbed")
DOWN_WORDS = ("down", "fell", "falls", "fallen", "decrease", "decreased", "decreases", "lower", "declined",
              "declines", "shrank", "narrowed", "eased", "slowed", "dropped", "cut")
FLAT_WORDS = ("unchanged", "flat", "stable", "steady", "held", "held at")
UP_RE = re.compile(r"\b(?:" + "|".join(UP_WORDS) + r")\b", re.IGNORECASE)
DOWN_RE = re.compile(r"\b(?:" + "|".join(DOWN_WORDS) + r")\b", re.IGNORECASE)
FLAT_RE = re.compile(r"\b(?:" + "|".join(FLAT_WORDS) + r")\b", re.IGNORECASE)
MONTHS_RE = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
                       re.IGNORECASE)


@dataclass
class ClaimRef:
    metric_ref: str
    dims: dict[str, Any]
    period: str
    basis: str
    raw: str

    @property
    def lookup_key(self) -> str:
        return self.metric_ref + (("|" + json.dumps(self.dims, sort_keys=True)) if self.dims else "")


@dataclass
class ResolvedClaim:
    ref: ClaimRef
    value: float | None
    unit: str | None
    period: str | None
    label: str | None
    source_doc_ids: list[str] = field(default_factory=list)
    problem: str | None = None


CLAIM_RE = re.compile(r"^(?P<metric>[^|@#]+)(?:\|(?P<dims>\{.*\}))?@(?P<period>[^#]+)(?:#(?P<basis>\w+))?$")


def parse_claim_id(claim_id: str) -> ClaimRef | None:
    m = CLAIM_RE.match((claim_id or "").strip())
    if not m:
        return None
    dims: dict[str, Any] = {}
    if m.group("dims"):
        try:
            dims = json.loads(m.group("dims"))
        except ValueError:
            return None
    basis = (m.group("basis") or "level").lower()
    return ClaimRef(m.group("metric").strip(), dims, m.group("period").strip(), basis, claim_id.strip())


def format_claim_id(metric_ref: str, period: str, basis: str = "level", dims: dict[str, Any] | None = None) -> str:
    base = metric_ref + (("|" + json.dumps(dims, sort_keys=True)) if dims else "")
    return f"{base}@{period}#{basis}"


def _basis_for_metric(entry: dict[str, Any]) -> set[str]:
    """Which claim bases a fact-pack entry may legitimately be written as."""
    unit = (entry.get("unit") or "").strip()
    ptype = (entry.get("period_type") or "").lower()
    ref = entry.get("id") or ""
    allowed = {"level"}
    if ptype == "forecast":
        # a projection may only be written as a projection; calling it a level presents it as measured
        allowed = {"forecast"}
    elif ptype == "stress_test_projection":
        allowed = {"stress"}
    elif ref.endswith(".yoy") or "growth_yoy" in ptype:
        allowed = {"yoy", "level"}
    elif ref.endswith(".mom") or ref.endswith(".mom_change"):
        allowed = {"mom", "change", "level"}
    elif ref.endswith(".share"):
        allowed = {"share", "level"}
    elif ref.endswith(".contrib"):
        allowed = {"contribution", "level"}
    elif ref.endswith(".ytd"):
        allowed = {"ytd", "level"}
    if unit == "%":
        allowed |= {"ratio"}
    return allowed


def primary_basis(entry: dict[str, Any]) -> str:
    """The basis a metric is normally written on.

    Chosen by what the metric *is*, not alphabetically: a month-on-month rate is written as `mom`,
    and picking `change` for it would silently resolve to the change in that rate instead.
    """
    ptype = (entry.get("period_type") or "").lower()
    ref = entry.get("id") or ""
    if ptype == "forecast":
        return "forecast"
    if ptype == "stress_test_projection":
        return "stress"
    for suffix, basis in ((".yoy", "yoy"), (".mom", "mom"), (".share", "share"), (".contrib", "contribution"),
                          (".ytd", "ytd")):
        if ref.endswith(suffix):
            return basis
    if "growth_yoy" in ptype:
        return "yoy"
    return "level"


def resolve(claim_id: str, fp: dict[str, Any]) -> ResolvedClaim:
    """Resolve a claim reference against the fact pack, checking every component."""
    ref = parse_claim_id(claim_id)
    if ref is None:
        return ResolvedClaim(ClaimRef(claim_id, {}, "", "level", claim_id), None, None, None, None,
                             problem=f"claim id {claim_id!r} is not in the form metric[|dims]@period#basis")
    if ref.basis not in BASES:
        return ResolvedClaim(ref, None, None, None, None, problem=f"unknown comparison basis {ref.basis!r}")
    metrics: dict[str, Any] = fp.get("metrics", {})
    entry = metrics.get(ref.lookup_key) or metrics.get(ref.metric_ref)
    if entry is None:
        return ResolvedClaim(ref, None, None, None, None, problem=f"metric {ref.lookup_key!r} is not in the fact pack")
    if ref.dims and entry.get("dims") and {k: str(v) for k, v in entry["dims"].items()} != {k: str(v) for k, v in ref.dims.items()}:
        return ResolvedClaim(ref, None, None, None, None,
                             problem=f"metric {ref.metric_ref!r} is published for dimensions {entry.get('dims')}, not {ref.dims}")
    allowed = _basis_for_metric(entry)
    if ref.basis not in allowed and ref.basis not in ("change", "pp_change", "bp_change"):
        return ResolvedClaim(ref, None, entry.get("unit"), None, entry.get("label"),
                             problem=f"{ref.metric_ref} may be written as {sorted(allowed)}, not as {ref.basis!r}")
    unit = entry.get("unit")
    value: float | None
    period: str | None
    if ref.basis in ("change", "pp_change", "bp_change"):
        value = entry.get("change")
        period = (entry.get("latest") or {}).get("period")
        if _normalise_unit(unit) in ("%", "pp"):
            unit = "pp"           # a move in a percentage is measured in percentage points
        if ref.basis == "bp_change" and value is not None:
            value, unit = value * 100.0, "bp"
    else:
        latest = entry.get("latest") or {}
        prior = entry.get("prior") or {}
        if latest.get("period") == ref.period:
            value, period = latest.get("value"), latest.get("period")
        elif prior.get("period") == ref.period:
            value, period = prior.get("value"), prior.get("period")
        else:
            value, period = _from_chart(entry, ref.period, fp)
            if value is None:
                return ResolvedClaim(ref, None, unit, None, entry.get("label"),
                                     problem=f"{ref.metric_ref} is published for {latest.get('period')}"
                                             f"{' and ' + prior['period'] if prior else ''}, not for {ref.period}")
    if value is None:
        return ResolvedClaim(ref, None, unit, period, entry.get("label"),
                             problem=f"{ref.metric_ref} has no value for {ref.period} on basis {ref.basis}")
    return ResolvedClaim(ref, float(value), unit, period, entry.get("label"), list(entry.get("doc_ids") or []))


def _from_chart(entry: dict[str, Any], period: str, fp: dict[str, Any]) -> tuple[float | None, str | None]:
    """Earlier periods of the same metric, taken from the chart series the deck actually plots."""
    ref = entry.get("id")
    for slide in (fp.get("slides") or {}).values():
        for value in (slide or {}).values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("id") == ref and item.get("points"):
                    for p, v in item["points"]:
                        if p == period and v is not None:
                            return float(v), p
    return None, None


# --------------------------------------------------------------------- text binding

@dataclass
class TextNumber:
    value: float
    unit: str | None
    written: str
    decimals: int
    start: int
    end: int


def numbers_in(text: str) -> list[TextNumber]:
    """Every number written in a piece of narrative text, with the unit it carries.

    Years are skipped; nothing else is. Small numbers are not exempt: "overdue loans rose 0.4 pp"
    is a financial claim and has to be grounded like any other.
    """
    out: list[TextNumber] = []
    for m in NUMBER_TOKEN.finditer(text or ""):
        raw = m.group("num")
        digits = raw.replace(",", "").replace(" ", "")
        if re.fullmatch(r"(19|20)\d{2}", digits) and not m.group("unit"):
            continue                                   # a calendar year, not a measurement
        before, after = text[max(0, m.start() - 24):m.start()], text[m.end():m.end() + 14]
        if not m.group("unit") and (MONTHS_RE.match(after.strip()) or MONTHS_RE.search(before[-12:])):
            continue                                   # the day in a date, not a measurement
        if re.search(r"\b(table|slide|appendix|page|section|figure|chart)\s*$", before, re.IGNORECASE):
            continue                                   # a reference to a numbered table or slide
        try:
            value = float(digits)
        except ValueError:
            continue
        if m.group("sign") in ("-", "−"):
            value = -value
        unit = UNIT_ALIASES.get((m.group("unit") or "").lower().rstrip(".")) if m.group("unit") else None
        if (m.group("unit") or "").lower().startswith("percentage point"):
            unit = "pp"
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append(TextNumber(value, unit, m.group(0).strip(), decimals, m.start(), m.end()))
    return out


def _scaled_candidates(value: float, unit: str | None) -> list[tuple[float, str]]:
    """The same fact written at another scale: 1,234 mln may be written as 1.2 bn."""
    out = [(value, unit or "")]
    if value is not None and unit in (None, "mln", "AZN mln", "USD mln"):
        out.append((value / 1000.0, "bn"))
    if value is not None and unit == "bn":
        out.append((value * 1000.0, "mln"))
    if unit == "pp":
        out.append((value * 100.0, "bp"))
    if unit == "bp":
        out.append((value / 100.0, "pp"))
    return out


def matches_value(written: TextNumber, claim: ResolvedClaim) -> bool:
    """Does the written number equal the claim's value at the precision it was written to?

    The tolerance is half of the last displayed digit and nothing more, so a rounded 12.9 matches
    12.94 but not 13.4. A sign written in words ("down 4.3 pp") is allowed to drop the minus.
    """
    if claim.value is None:
        return False
    tol = 0.5 * 10 ** (-written.decimals) + 1e-9
    unit = _normalise_unit(claim.unit)
    for value, cand_unit in _scaled_candidates(claim.value, unit):
        if written.unit and cand_unit and _normalise_unit(written.unit) != _normalise_unit(cand_unit):
            continue
        if abs(value - written.value) <= tol:
            return True
        if abs(abs(value) - abs(written.value)) <= tol:     # "fell 4.3 pp" for a -4.3 pp change
            return True
    return False


def _normalise_unit(unit: str | None) -> str:
    u = (unit or "").strip().lower()
    if u.startswith("%"):            # "%", "% p.a.", "% of assets" are all percentages
        return "%"
    if u == "percent":
        return "%"
    if u in ("pp", "p.p.", "percentage point", "percentage points"):
        return "pp"
    if u in ("bp", "bps", "basis points"):
        return "bp"
    if u.endswith("mln"):
        return "mln"
    if u.endswith("bn"):
        return "bn"
    return u


def unit_consistent(written: TextNumber, claim: ResolvedClaim) -> bool:
    """A percentage change may not be written as a percentage, and vice versa."""
    if written.unit is None:
        return True
    w, c = _normalise_unit(written.unit), _normalise_unit(claim.unit)
    if w == c:
        return True
    if {w, c} == {"pp", "bp"}:
        return True
    if {w, c} == {"mln", "bn"}:
        return True
    return False


def direction_ok(text: str, written: TextNumber, claim: ResolvedClaim) -> str | None:
    """Check a direction word next to the number against the sign of the claim.

    Only claims that are themselves a movement carry a direction; a level has none, so a sentence
    may describe a level as "up" only when a separate change claim supports it.
    """
    if claim.ref.basis not in ("change", "pp_change", "bp_change", "yoy", "mom"):
        return None
    if claim.value is None:
        return None
    window = text[max(0, written.start - 70):written.end + 30]
    said_up, said_down, said_flat = bool(UP_RE.search(window)), bool(DOWN_RE.search(window)), bool(FLAT_RE.search(window))
    if said_up and said_down:
        return None                                   # a comparative sentence; no single direction asserted
    if said_up and claim.value < 0:
        return f"text says the value rose but {claim.ref.metric_ref} is {claim.value:+.2f}"
    if said_down and claim.value > 0:
        return f"text says the value fell but {claim.ref.metric_ref} is {claim.value:+.2f}"
    # "unchanged" is only a contradiction for a movement; a growth rate can be stable at a non-zero value
    if said_flat and claim.ref.basis in ("change", "pp_change", "bp_change") and abs(claim.value) > 0.05:
        return f"text calls the value unchanged but {claim.ref.metric_ref} moved {claim.value:+.2f}"
    return None


def claim_for(entry: dict[str, Any] | None, which: str = "latest") -> str | None:
    """The claim id for a fact-pack entry's latest value, prior value or change."""
    if not entry:
        return None
    metric_ref = entry.get("id")
    dims = entry.get("dims") or {}
    if not metric_ref:
        return None
    if which == "change":
        period = (entry.get("latest") or {}).get("period")
        return format_claim_id(metric_ref, period, "change", dims) if period and entry.get("change") is not None else None
    node = entry.get(which) or {}
    if not node.get("period") or node.get("value") is None:
        return None
    return format_claim_id(metric_ref, node["period"], primary_basis(entry), dims)


def catalogue(fp: dict[str, Any]) -> list[dict[str, Any]]:
    """Every claim a narrative may make about this fact pack, with the token to write.

    The commentary routine writes from this list, so a claim id is never invented by hand.
    """
    # One entry per claim id: a metric registered under several fact-pack keys (a scorecard row and
    # a slide alias, say) is one claim, not several candidates.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for key, entry in (fp.get("metrics") or {}).items():
        latest, prior = entry.get("latest") or {}, entry.get("prior") or {}
        metric_ref = entry.get("id") or key.split("|")[0]
        dims = entry.get("dims") or {}
        primary = primary_basis(entry)
        if latest.get("period") and latest.get("value") is not None and \
                format_claim_id(metric_ref, latest["period"], primary, dims) not in seen:
            seen.add(format_claim_id(metric_ref, latest["period"], primary, dims))
            out.append({"claim_id": format_claim_id(metric_ref, latest["period"], primary, dims),
                        "label": entry.get("label"), "value": latest["value"], "unit": entry.get("unit"),
                        "period": latest["period"], "period_label": latest.get("period_label"), "basis": primary,
                        "compare": entry.get("compare"), "source_doc_ids": (entry.get("doc_ids") or [])[:4]})
        if prior.get("period") and prior.get("value") is not None and \
                format_claim_id(metric_ref, prior["period"], primary, dims) not in seen:
            seen.add(format_claim_id(metric_ref, prior["period"], primary, dims))
            out.append({"claim_id": format_claim_id(metric_ref, prior["period"], primary, dims),
                        "label": entry.get("label"), "value": prior["value"], "unit": entry.get("unit"),
                        "period": prior["period"], "period_label": prior.get("period_label"), "basis": primary,
                        "compare": entry.get("compare"), "source_doc_ids": (entry.get("doc_ids") or [])[:4]})
        if entry.get("change") is not None and latest.get("period") and \
                format_claim_id(metric_ref, latest["period"], "change", dims) not in seen:
            seen.add(format_claim_id(metric_ref, latest["period"], "change", dims))
            unit = "pp" if (entry.get("unit") in ("%", "pp")) else entry.get("unit")
            out.append({"claim_id": format_claim_id(metric_ref, latest["period"], "change", dims),
                        "label": (entry.get("label") or "") + " — change vs the comparison period",
                        "value": entry["change"], "unit": unit, "period": latest["period"],
                        "period_label": latest.get("period_label"), "basis": "change",
                        "compare": entry.get("compare"), "source_doc_ids": (entry.get("doc_ids") or [])[:4]})
    return out
