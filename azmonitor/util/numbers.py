"""Normalisation of Azerbaijani number formats and source missing-value markers.

Rules (from the SSC report legend and CBA conventions):
  "-"     event did not occur (published zero-like marker, kept as missing with reason "dash")
  "…"     data not available
  "0,0"   very small value (kept as 0.0 with flag)
  "x"     comparison not possible
  "2,8 d." growth expressed as a multiple ("dəfə" = times) -> converted to percent growth
  Decimal comma, spaces / non-breaking spaces as thousands separators, leading "+".
Never fill a missing value with zero.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_MISSING = {
    "": "blank",
    "-": "dash",
    "–": "dash",
    "—": "dash",
    "…": "not_available",
    "...": "not_available",
    "x": "not_comparable",
    "х": "not_comparable",  # cyrillic x
    "n/a": "not_available",
    "#ref!": "spreadsheet_error",
    "#n/a": "spreadsheet_error",
    "#div/0!": "spreadsheet_error",
}

_TIMES_RE = re.compile(r"^([+-]?\d[\d\s .,]*)\s*(d\.|dəfə|раз|times|x)$", re.IGNORECASE)


@dataclass
class Parsed:
    value: float | None
    raw: str
    missing_reason: str | None = None
    flags: list[str] = field(default_factory=list)
    is_percent: bool = False


def parse_number(value) -> Parsed:
    """Parse a spreadsheet/PDF cell into a float with provenance flags."""
    if value is None:
        return Parsed(None, "", "blank")
    if isinstance(value, bool):
        return Parsed(float(value), str(value), None, ["boolean"])
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return Parsed(None, str(value), "nan")
        return Parsed(float(value), repr(value))
    raw = str(value)
    s = raw.strip().replace(" ", " ").replace(" ", " ")
    low = s.lower()
    if low in _MISSING:
        return Parsed(None, raw, _MISSING[low])
    flags: list[str] = []
    is_percent = False
    m = _TIMES_RE.match(s)
    if m:
        base = parse_number(m.group(1))
        if base.value is None:
            return Parsed(None, raw, "unparseable")
        flags.append("multiple_converted_to_percent_growth")
        return Parsed((base.value - 1.0) * 100.0, raw, None, flags, True)
    if s.endswith("%"):
        is_percent = True
        s = s[:-1].strip()
    # footnote markers like "107,01)" or "1 176,7 2)" -> strip trailing "<digit>)" groups
    s2 = re.sub(r"(\d)\s?\d\)$", r"\1", s)
    if s2 != s:
        flags.append("footnote_marker_stripped")
        s = s2
    s = s.replace("*", "")
    # unicode minus
    s = s.replace("−", "-").replace("–", "-")
    # remove spaces used as thousands separators
    compact = re.sub(r"(?<=\d)[  ](?=\d{3}\b)", "", s)
    compact = compact.replace(" ", "")
    # decide decimal separator
    if "," in compact and "." in compact:
        # assume '.' thousands and ',' decimal when comma is last, else the reverse
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif "," in compact:
        parts = compact.split(",")
        if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3 and not parts[0].startswith(("+", "-", "0")):
            # ambiguous "1,234": Azerbaijani sources use comma as decimal; keep as decimal but flag
            flags.append("ambiguous_comma_treated_as_decimal")
        compact = compact.replace(",", ".")
    compact = compact.lstrip("+")
    try:
        val = float(compact)
    except ValueError:
        return Parsed(None, raw, "unparseable")
    if val == 0.0 and re.match(r"^[+-]?0[.,]0+$", s.strip()):
        flags.append("published_zero_marker")
    return Parsed(val, raw, None, flags, is_percent)


def index_to_growth(index_value: float | None) -> float | None:
    """Convert an index expressed as percent of the comparison base (e.g. 101,2) to growth in percent."""
    if index_value is None:
        return None
    return index_value - 100.0
