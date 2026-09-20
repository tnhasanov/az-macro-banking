"""Monetary policy extraction: decisions, the interest rate corridor, forecasts and stance.

Two independent sources are used and kept apart:

* the *decision press release* gives the announcement date, the Central Bank's own rationale and,
  when it states one, the date the decision takes effect. It is the authority for why;
* the *monetary policy review* prints a table of recent decisions with the refinancing rate and both
  corridor bounds, including "6.25%→6.00%" arrows on the meetings that changed them. It is the
  authority for the levels.

Rates are therefore attributed to the review table, rationale to the press release, and neither is
inferred from the other. A decision whose level is not covered by any published table keeps a null
rate rather than a carried-forward one.

Forecasts are only extracted from sentences that name both the variable and the target period. Each
one records the vintage it was published in, the target period, the definition and the scenario, so
a later comparison can line up like with like. Nothing is read out of a chart.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..parsers.base import Observation
from ..util.log import get_logger
from ..util.periods import az_month_number, month_end
from .editions import EN_MONTHS
from .textutil import denoise as _denoise, flow as _flow, passage_at as _passage_at, sentence_spans as _sentence_spans

log = get_logger("policy")

AZ_MONTHS_RE = r"yanvar|fevral|mart|aprel|may|iyun|iyul|avqust|sentyabr|oktyabr|noyabr|dekabr"
EN_MONTHS_RE = "|".join(EN_MONTHS)
ARROW = "→"

# "22 yanvar, 2025" / "22 January 2025" / "22 January, 2025"
DEC_DATE_RE = re.compile(rf"^\s*(\d{{1,2}})\s+({AZ_MONTHS_RE}|{EN_MONTHS_RE})\s*,?\s*(\d{{4}})\s*$", re.IGNORECASE)
PCT_RE = re.compile(r"(-?\d{1,2}(?:[.,]\d{1,2})?)\s*%")
HEADER_HINTS = {
    "floor": ("aşağı həd", "aşağı hədd", "floor", "lower bound", "lower limit", "deposit"),
    "rate": ("uçot dərəcəsi", "refinancing", "policy rate", "discount rate", "key rate"),
    "ceiling": ("yuxarı həd", "yuxarı hədd", "ceiling", "upper bound", "upper limit"),
}


@dataclass
class DecisionRow:
    date: dt.date
    floor: float | None = None
    rate: float | None = None
    ceiling: float | None = None
    floor_prev: float | None = None
    rate_prev: float | None = None
    ceiling_prev: float | None = None
    passage_id: str | None = None
    page: int | None = None

    @property
    def changed(self) -> bool:
        return any(p is not None for p in (self.rate_prev, self.floor_prev, self.ceiling_prev))

    def change_bp(self, which: str) -> float | None:
        new, old = getattr(self, which), getattr(self, f"{which}_prev")
        if new is None:
            return None
        if old is None:
            return 0.0
        return round((new - old) * 100.0, 1)


def _num(token: str) -> float | None:
    m = PCT_RE.search(token.replace(",", "."))
    if not m:
        m = re.search(r"(-?\d{1,2}(?:\.\d{1,2})?)", token.replace(",", "."))
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _cell_pair(cell: str) -> tuple[float | None, float | None]:
    """Return (new, previous). "6.25%→6.00%" means the meeting moved the rate from 6.25 to 6.00."""
    text = (cell or "").replace("->", ARROW).replace("=>", ARROW)
    if ARROW in text:
        before, after = text.split(ARROW, 1)
        return _num(after), _num(before)
    return _num(text), None


def _decision_date(token: str) -> dt.date | None:
    m = DEC_DATE_RE.match((token or "").strip())
    if not m:
        return None
    day, mon_token, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mon = EN_MONTHS.get(mon_token) or az_month_number(mon_token)
    if not mon:
        return None
    try:
        return dt.date(year, mon, day)
    except ValueError:
        return None


def parse_decision_table(passages: Iterable[Any]) -> list[DecisionRow]:
    """Read the 'recent monetary policy decisions' table printed in every policy review.

    Columns are matched on their headers, never on position, because the Azerbaijani and English
    editions order the corridor bounds differently in some years.
    """
    rows: list[DecisionRow] = []
    for p in passages:
        kind = p["kind"] if isinstance(p, dict) else p.kind
        if kind != "table":
            continue
        text = p["text"] if isinstance(p, dict) else p.text
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if len(lines) < 3:
            continue
        header = lines[0].lower()
        idx: dict[str, int] = {}
        cells = [c.strip() for c in header.split("|")]
        for key, hints in HEADER_HINTS.items():
            for j, c in enumerate(cells):
                if any(h in c for h in hints):
                    idx[key] = j
                    break
        if "rate" not in idx or len(idx) < 2:
            continue
        pid = p["passage_id"] if isinstance(p, dict) else None
        page = p["page_index"] if isinstance(p, dict) else p.page_index
        found = 0
        for ln in lines[1:]:
            cols = [c.strip() for c in ln.split("|")]
            if not cols:
                continue
            date = next((d for d in (_decision_date(c) for c in cols[:2]) if d), None)
            if date is None:
                continue
            row = DecisionRow(date=date, passage_id=pid, page=page)
            for key in ("floor", "rate", "ceiling"):
                j = idx.get(key)
                if j is None or j >= len(cols):
                    continue
                new, prev = _cell_pair(cols[j])
                setattr(row, key, new)
                setattr(row, f"{key}_prev", prev)
            if row.rate is None:
                continue
            rows.append(row)
            found += 1
        if found:
            log.debug("decision table: %d rows on page %s", found, page)
    # de-duplicate by date, keeping the row with the most detail
    best: dict[dt.date, DecisionRow] = {}
    for r in rows:
        cur = best.get(r.date)
        score = sum(x is not None for x in (r.floor, r.rate, r.ceiling)) + (1 if r.changed else 0)
        if cur is None or score > sum(x is not None for x in (cur.floor, cur.rate, cur.ceiling)) + (1 if cur.changed else 0):
            best[r.date] = r
    return [best[k] for k in sorted(best)]


def decision_observations(rows: list[DecisionRow], *, publication_id: str, language: str) -> list[Observation]:
    """Policy rate and corridor levels as an event series dated by the decision they come from.

    No monthly series is manufactured from these: a decision holds until the next one, and a chart
    that needs a step line draws it from these points.
    """
    out: list[Observation] = []
    for r in rows:
        for series, value, label in (("cba.policy.rate", r.rate, "Refinancing rate"),
                                     ("cba.policy.corridor_floor", r.floor, "Interest rate corridor floor"),
                                     ("cba.policy.corridor_ceiling", r.ceiling, "Interest rate corridor ceiling")):
            if value is None:
                continue
            out.append(Observation(
                series_id=series, period_end=r.date, period_start=r.date, value=value, value_raw=f"{value}%",
                freq="E", period_type="policy_rate_effective", unit="%", source_id="CBA_POLICY",
                label_original=label, extraction_method="pdf_table", basis="level decided at the meeting on this date",
                publication_id=publication_id, passage_id=r.passage_id, validation_status="verified",
                announced_at=r.date, language=language, cell_ref=f"decisions table p{r.page}",
            ))
    return out


# --------------------------------------------------------------------- press release

# A decision statement may *reschedule* the next meeting ("changed from August 5, 2026, to July 31"),
# so every date in the sentence is collected and the last one is taken.
AZ_DATE_PATTERNS = (
    rf"(?P<y>\d{{4}})-c[iıuü] il\s+(?P<m>{AZ_MONTHS_RE})\s+ay[ıi]n[ıi]n\s+(?P<d>\d{{1,2}})",
    rf"(?P<d>\d{{1,2}})\s+(?P<m>{AZ_MONTHS_RE})\s+(?P<y>\d{{4}})",
    rf"(?P<m>{AZ_MONTHS_RE})\s+ay[ıi]n[ıi]n\s+(?P<d>\d{{1,2}})",
)
EN_DATE_PATTERNS = (
    rf"(?P<d>\d{{1,2}})\s+(?P<m>{EN_MONTHS_RE})\s+(?P<y>\d{{4}})",
    rf"(?P<m>{EN_MONTHS_RE})\s+(?P<d>\d{{1,2}}),?\s*(?P<y>\d{{4}})",
    rf"(?P<m>{EN_MONTHS_RE})\s+(?P<d>\d{{1,2}})\b",
)
EFFECTIVE_AZ = re.compile(rf"(\d{{1,2}})\s+({AZ_MONTHS_RE})\s+(\d{{4}})[^.]{{0,40}}?(?:tarixind[əe]n|-d[əe]n)\s+(?:etibar[əe]n\s+)?q[üu]vv[əe]y[əe]\s+min", re.IGNORECASE)
EFFECTIVE_EN = re.compile(rf"(?:effective|enters? into force|shall take effect)\s+(?:from|on|as of)?\s*(\d{{1,2}})\s+({EN_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE)
ACTION_WORDS = {
    "cut": ("azald", "endiril", "cut", "lower", "reduce", "decreas"),
    "raise": ("artırıl", "yüksəldil", "raise", "increas", "hike"),
    "hold": ("dəyişməz", "sabit saxlan", "unchanged", "keep", "kept", "maintain"),
}


@dataclass
class DecisionRelease:
    announcement_date: dt.date | None = None
    effective_date: dt.date | None = None
    effective_basis: str | None = None
    action: str | None = None
    rationale: str = ""
    rationale_passages: list[str] = field(default_factory=list)
    next_decision_date: dt.date | None = None
    next_decision_basis: str | None = None
    language: str = "az"
    warnings: list[str] = field(default_factory=list)


def _dates_in(sentence: str, language: str, default_year: int | None) -> list[dt.date]:
    """Every date mentioned in a sentence, in the order they appear."""
    found: list[tuple[int, dt.date]] = []
    taken: list[tuple[int, int]] = []
    for pat in (AZ_DATE_PATTERNS if language == "az" else EN_DATE_PATTERNS):
        for m in re.finditer(pat, sentence, re.IGNORECASE):
            if any(lo <= m.start() < hi for lo, hi in taken):
                continue
            gd = m.groupdict()
            mon = az_month_number(gd["m"]) if language == "az" else EN_MONTHS.get(gd["m"].lower())
            year = int(gd["y"]) if gd.get("y") else default_year
            if not mon or not year:
                continue
            try:
                found.append((m.start(), dt.date(year, mon, int(gd["d"]))))
                taken.append((m.start(), m.end()))
            except ValueError:
                continue
    return [d for _, d in sorted(found)]


def _date_en(m) -> dt.date | None:
    try:
        return dt.date(int(m.group(3)), EN_MONTHS[m.group(2).lower()], int(m.group(1)))
    except (ValueError, KeyError):
        return None


def _date_az(m) -> dt.date | None:
    mon = az_month_number(m.group(2))
    try:
        return dt.date(int(m.group(1)), mon, int(m.group(3))) if mon else None
    except ValueError:
        return None


def parse_press_release(passages: list[dict[str, Any]], language: str, announced: dt.date | None = None) -> DecisionRelease:
    rel = DecisionRelease(announcement_date=announced, language=language)
    texts = [p["text"] for p in passages]
    body = " ".join(texts)
    head = " ".join(texts[:2]).lower()
    for action, words in ACTION_WORDS.items():
        if any(w in head for w in words):
            rel.action = action
            break
    if rel.action is None:
        for action, words in ACTION_WORDS.items():
            if any(w in body.lower() for w in words):
                rel.action = action
                break
    # rationale: the substantive paragraphs, excluding the heading
    rel.rationale_passages = [p["passage_id"] for p in passages if p["kind"] == "text"][:6]
    rel.rationale = " ".join(t for p, t in zip(passages, texts) if p["kind"] == "text")[:4000]
    # the next decision date is announced in the closing sentence
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if re.search(r"növbəti|next decision|next meeting|növbəti qərar", s, re.IGNORECASE)]
    for sentence in sentences:
        dates = [d for d in _dates_in(sentence, language, announced.year if announced else None)
                 if announced is None or d > announced]
        if not dates:
            continue
        rescheduled = bool(re.search(r"changed from|dəyişdiril|yerin[əe] keçiril", sentence, re.IGNORECASE))
        rel.next_decision_date = dates[-1]
        rel.next_decision_basis = ("announced in the decision statement (date rescheduled in the same sentence; "
                                   "the final date is used)" if rescheduled else "announced in the decision statement")
        if rescheduled:
            rel.warnings.append(f"next decision date rescheduled in the statement: {sentence[:160]}")
        break
    m = (EFFECTIVE_AZ if language == "az" else EFFECTIVE_EN).search(body)
    if m:
        d = _date_az(m) if language == "az" else _date_en(m)
        if d:
            rel.effective_date, rel.effective_basis = d, "stated in the decision statement"
    if rel.effective_date is None:
        rel.effective_basis = "not stated in the decision statement"
    return rel


# ------------------------------------------------------------------------ forecasts

FORECAST_DEFS = {
    "inflation": {"series": "cba.forecast.inflation", "unit": "%", "label": "CBA inflation forecast",
                  "definition": "annual consumer price inflation, end of period, baseline scenario"},
    "gdp": {"series": "cba.forecast.gdp_growth", "unit": "%", "label": "CBA GDP growth forecast",
            "definition": "real GDP growth for the calendar year, baseline scenario"},
    "gdp_nonoil": {"series": "cba.forecast.gdp_nonoil_growth", "unit": "%", "label": "CBA non-oil GDP growth forecast",
                   "definition": "real non-oil-gas GDP growth for the calendar year, baseline scenario"},
}
# "2026-cı ilin sonunda 6.1%" / "at the end of 2026 ... 6.1%"
AZ_EOY = re.compile(r"(\d{4})-c[iıuü] ilin sonunda\s+(?:is[əe]\s+)?(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
EN_EOY = re.compile(r"(?:at the end of|by the end of|end[- ]?)(\d{4})[^.%]{0,40}?(\d{1,2}(?:\.\d{1,2})?)\s*%")
AZ_YEAR_PAIR = re.compile(r"(\d{4})-c[iıuü] ild[əe]\s+(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
RANGE_HINT = re.compile(r"aral[ıi]ğ[ıi]nda|between\s+\d|in the range")
# a horizon is only accepted when the document itself names the target month, so that a
# "12 months ahead" figure is never dated from an assumed base month
AZ_MONTHS_AHEAD = re.compile(rf"\d{{1,2}}\s+ay sonra\s*\(?\s*(?:yəni)?\s*(\d{{4}})-c[iıuü] ilin\s+({AZ_MONTHS_RE})\s+ay[ıi]nda\)?[^%]{{0,20}}?(\d{{1,2}}(?:[.,]\d{{1,2}})?)\s*%", re.IGNORECASE)
EN_MONTHS_AHEAD = re.compile(rf"\d{{1,2}}\s+months? ahead\s*\(?\s*(?:i\.e\.?)?\s*(?:in\s+)?({EN_MONTHS_RE})\s+(\d{{4}})\)?[^%]{{0,20}}?(\d{{1,2}}(?:\.\d{{1,2}})?)\s*%", re.IGNORECASE)
NOISE_TOKEN = re.compile(r"(?<!\S)-?\d+(?:[.,]\d+)?(?!\S)(?!\s+(?:ay|ayda|ay[ıi]n|ay[ıi]nda|il|ild[əe]|ilin|month|months|year|f\.b\.|faiz|pp|p\.p\.))",
                         re.IGNORECASE)
# The gap between the variable and its number must be a short connective ("üzrə isə"), not a
# run of words: interleaved two-column text otherwise joins an unrelated figure to the anchor.
GROWTH_RE = re.compile(r"(ümumi ÜDM|qeyri[- ]neft[- ]qaz ÜDM|ÜDM|non[- ]oil(?:[- ]gas)? GDP|overall GDP|GDP)"
                       r"(?P<gap>[^%\d]{0,28}?)(-?\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.IGNORECASE)
MAX_GAP_WORDS = 3
# "2027-ci ildə ... ümumi və qeyri-neft-qaz sektoru üzrə real ifadədə müvafiq olaraq 2.6% və 3.9%"
AZ_GROWTH_PAIR = re.compile(r"ümumi\s+və\s+qeyri[- ]neft[- ]qaz[^%]{0,60}?müvafiq olaraq\s+(-?\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*və\s*(-?\d{1,2}(?:[.,]\d{1,2})?)\s*%",
                            re.IGNORECASE)


def _target_end(year: int) -> dt.date:
    return dt.date(year, 12, 31)


def extract_forecasts(passages: list[dict[str, Any]], language: str, *, publication_id: str,
                      vintage: str, vintage_date: dt.date | None) -> tuple[list[Observation], list[str]]:
    """Extract CBA projections from sentences that name the variable, the value and the target period.

    Deliberately conservative: a sentence that gives a range, or that does not tie its number to a
    named period, is recorded as a note and left to the passage record instead of becoming a false
    precise observation. Nothing is read from a chart.
    """
    out: list[Observation] = []
    notes: list[str] = []
    flow, spans = _flow(passages)
    for offset, raw_sent in _sentence_spans(flow):
        sent = _denoise(raw_sent)
        low = sent.lower()
        if not re.search(r"proqnoz|gözlənilir|projected|forecast|expected", low):
            continue
        is_inflation = "inflyasiya" in low or "inflation" in low
        is_growth = "ÜDM" in sent or "gdp" in low or "iqtisadi artım" in low or "economic growth" in low
        if not (is_inflation or is_growth):
            continue
        p = _passage_at(spans, offset) or {"page_index": None, "printed_page": None, "passage_id": None}
        if RANGE_HINT.search(low):
            notes.append(f"range statement on page {p.get('page_index')} left as evidence, not extracted as a point forecast")
            continue
        if is_inflation:
            for m in (AZ_EOY if language == "az" else EN_EOY).finditer(sent):
                year, val = int(m.group(1)), float(m.group(2).replace(",", "."))
                if 0.0 <= val <= 30.0:
                    out.append(_forecast_obs("inflation", val, _target_end(year), f"end-{year}", publication_id,
                                             _passage_at(spans, offset + m.start()) or p, vintage, vintage_date, language, m.group(0)))
            for m in (AZ_MONTHS_AHEAD if language == "az" else EN_MONTHS_AHEAD).finditer(sent):
                if language == "az":
                    year, mon, val = int(m.group(1)), az_month_number(m.group(2)), float(m.group(3).replace(",", "."))
                else:
                    mon, year, val = EN_MONTHS.get(m.group(1).lower()), int(m.group(2)), float(m.group(3))
                if not mon or not (0.0 <= val <= 30.0):
                    continue
                target = month_end(year, mon)
                out.append(_forecast_obs("inflation", val, target, f"{target.strftime('%b %Y')} (mid-horizon)", publication_id,
                                         _passage_at(spans, offset + m.start()) or p, vintage, vintage_date, language, m.group(0)))
        if is_growth and language == "az":
            for m in AZ_GROWTH_PAIR.finditer(sent):
                year = _growth_year(sent, m.start(), vintage_date)
                if year is None:
                    notes.append(f"a paired growth statement on page {p.get('page_index')} names no target year and was not extracted")
                    continue
                for key, raw in (("gdp", m.group(1)), ("gdp_nonoil", m.group(2))):
                    val = float(raw.replace(",", "."))
                    if -20.0 <= val <= 20.0:
                        out.append(_forecast_obs(key, val, _target_end(year), str(year), publication_id,
                                                 _passage_at(spans, offset + m.start()) or p, vintage, vintage_date, language, m.group(0)))
        if is_growth and ("ÜDM" in sent or "gdp" in low):
            for m in GROWTH_RE.finditer(sent):
                key = "gdp_nonoil" if re.search(r"qeyri|non[- ]oil", m.group(1), re.IGNORECASE) else "gdp"
                gap = m.group("gap") or ""
                if len(gap.split()) > MAX_GAP_WORDS:
                    notes.append(f"a growth figure on page {p.get('page_index')} was separated from its label by "
                                 f"interrupted text and was not extracted")
                    continue
                val = float(m.group(3).replace(",", "."))
                year = _growth_year(sent, m.start(), vintage_date)
                if year is None:
                    notes.append(f"growth figure without a named target year on page {p.get('page_index')} not extracted")
                    continue
                if not (-20.0 <= val <= 20.0):
                    continue
                out.append(_forecast_obs(key, val, _target_end(year), str(year), publication_id,
                                         _passage_at(spans, offset + m.start()) or p, vintage, vintage_date, language, m.group(0)))
    dedup: dict[tuple[str, str], Observation] = {}
    for o in out:
        dedup[(o.series_id, o.period_end.isoformat())] = o
    return list(dedup.values()), sorted(set(notes))


def _growth_year(text: str, pos: int, vintage_date: dt.date | None) -> int | None:
    """The year a growth figure refers to: an explicit year in the same clause, else 'this year'."""
    window = text[max(0, pos - 220):pos + 60]
    years = re.findall(r"(20\d{2})-c[iıuü] ild[əe]|in (20\d{2})|for (20\d{2})", window)
    flat = [int(y) for grp in years for y in grp if y]
    if flat:
        return flat[-1]
    if re.search(r"cari ild[əe]|this year|current year", window, re.IGNORECASE) and vintage_date:
        return vintage_date.year
    return None


def _forecast_obs(key: str, value: float, target: dt.date, horizon: str, publication_id: str,
                  passage: dict[str, Any], vintage: str, vintage_date: dt.date | None, language: str,
                  raw: str) -> Observation:
    spec = FORECAST_DEFS[key]
    return Observation(
        series_id=spec["series"], period_end=target, period_start=dt.date(target.year, 1, 1), value=value, value_raw=raw,
        freq="A", period_type="forecast", unit=spec["unit"], dims={"vintage": vintage, "scenario": "baseline"},
        source_id="CBA_POLICY", label_original=spec["label"], basis=spec["definition"], extraction_method="pdf_text_pattern",
        publication_id=publication_id, passage_id=passage.get("passage_id"), validation_status="verified",
        scenario="baseline", forecast_vintage=vintage, horizon_label=horizon, language=language,
        cell_ref=f"page {passage['page_index']}" + (f" (printed {passage['printed_page']})" if passage.get("printed_page") else ""),
    )


def stance_comparison(current: DecisionRow | None, previous: DecisionRow | None,
                      current_rel: DecisionRelease | None, previous_rel: DecisionRelease | None) -> dict[str, Any]:
    """Previous assessment -> current assessment -> evidence of change.

    The stance label describes the *rate decision only*; an unchanged corridor with a changed
    rationale is reported as "unchanged rate, changed rationale", never as a tightening.
    """
    out: dict[str, Any] = {"rate_change_bp": None, "corridor_change_bp": None, "label": "not established",
                           "evidence": [], "rationale_changed": None}
    if current is None:
        return out
    out["rate_change_bp"] = current.change_bp("rate")
    out["corridor_change_bp"] = current.change_bp("ceiling") if current.ceiling is not None else None
    if previous is not None and current.rate is not None and previous.rate is not None:
        delta = round((current.rate - previous.rate) * 100.0, 1)
        out["rate_change_bp"] = delta
        out["evidence"].append(f"refinancing rate {previous.rate:.2f}% on {previous.date.isoformat()} to "
                               f"{current.rate:.2f}% on {current.date.isoformat()}")
    delta = out["rate_change_bp"]
    if delta is None:
        out["label"] = "not established"
    elif delta < 0:
        out["label"] = "eased"
    elif delta > 0:
        out["label"] = "tightened"
    else:
        out["label"] = "rate unchanged"
    if current_rel and previous_rel and current_rel.rationale and previous_rel.rationale:
        cur_words = set(re.findall(r"[a-zçəğıöşü]{5,}", current_rel.rationale.lower()))
        prev_words = set(re.findall(r"[a-zçəğıöşü]{5,}", previous_rel.rationale.lower()))
        if cur_words or prev_words:
            overlap = len(cur_words & prev_words) / max(1, len(cur_words | prev_words))
            out["rationale_changed"] = overlap < 0.6
            out["rationale_overlap"] = round(overlap, 3)
            if delta == 0 and out["rationale_changed"]:
                out["label"] = "rate unchanged, rationale changed"
    return out


def compare_forecasts(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Revisions between two forecast vintages, matched on series *and* target period.

    Forecasts for different horizons are never differenced against each other.
    """
    prev_index = {(f["series_id"], f["period_end"], f.get("scenario") or "baseline"): f for f in previous}
    out = []
    for f in current:
        key = (f["series_id"], f["period_end"], f.get("scenario") or "baseline")
        p = prev_index.get(key)
        row = {"series_id": f["series_id"], "target_period": f["period_end"], "horizon": f.get("horizon_label"),
               "scenario": f.get("scenario") or "baseline", "definition": f.get("basis"),
               "current": f["value"], "current_vintage": f.get("forecast_vintage"),
               "previous": p["value"] if p else None, "previous_vintage": p.get("forecast_vintage") if p else None,
               "revision_pp": round(f["value"] - p["value"], 2) if p and p["value"] is not None and f["value"] is not None else None}
        row["comparable"] = p is not None
        out.append(row)
    return sorted(out, key=lambda r: (r["series_id"], r["target_period"]))
