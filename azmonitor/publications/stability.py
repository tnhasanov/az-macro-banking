"""Financial stability extraction from the CBA's Financial Stability Report.

The report is the only public source for several measures a CRO needs and the monthly tables never
carry: regulatory capital adequacy, the liquidity coverage ratio, and system stress-test results.
Three distinctions are preserved everywhere, because collapsing any of them would mislead:

* regulatory capital adequacy is not accounting equity over assets. They are separate series and
  are never mixed in one chart or one sentence;
* an observed outcome is not a stress-test result. Stress figures carry their scenario and the
  starting date of the exercise, use a period type of their own, and are never called forecasts;
* an aggregate is not a distribution. A sector-wide ratio is stored with its population, and a
  minimum or maximum across banks is stored as a separate series.

Values are read from running text with anchored patterns. A figure that appears only in a chart is
left in the passage record as evidence and never becomes an observation.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from ..parsers.base import Observation
from ..util.log import get_logger
from .textutil import cite, denoise, flow, passage_at, sentence_spans

log = get_logger("stability")

VERB_EN = r"(?:stood at|stands at|reached|amounted to|was|were|is|of|at|to|equalled|equaled|remained at|increased to|decreased to|declined to|rose to)"
VERB_AZ = r"(?:təşkil|olmuşdur|olub|çatmışdır|yüksəlmişdir|azalmışdır|bərabər)"


@dataclass(frozen=True)
class Indicator:
    key: str
    series_id: str
    label_en: str
    unit: str
    anchors_en: tuple[str, ...]
    anchors_az: tuple[str, ...]
    definition: str
    population: str = "banking sector (CBA prudential)"
    # how far a reading may sit from the median of the other editions before it is held for review
    max_deviation_pp: float = 6.0


INDICATORS: tuple[Indicator, ...] = (
    Indicator("car", "cba.fsr.car", "Capital adequacy ratio (regulatory)", "%",
              ("capital adequacy ratio", "total capital adequacy", "car"),
              ("kapital adekvatlığı əmsalı", "məcmu kapitalın adekvatlıq əmsalı", "kapitalın adekvatlıq əmsalı"),
              "regulatory total capital / risk-weighted assets, prudential reporting; not accounting equity over assets",
              max_deviation_pp=8.0),
    Indicator("tier1", "cba.fsr.tier1_ratio", "Tier 1 capital adequacy ratio", "%",
              ("tier 1 capital adequacy", "tier i capital adequacy", "tier 1 ratio"),
              ("i dərəcəli kapitalın adekvatlıq", "birinci dərəcəli kapitalın adekvatlıq"),
              "regulatory Tier 1 capital / risk-weighted assets"),
    Indicator("lcr", "cba.fsr.lcr", "Liquidity coverage ratio", "%",
              ("liquidity coverage ratio", "lcr"),
              ("likvidliyin ödənilmə əmsalı", "likvidliyin örtülmə əmsalı"),
              "high-quality liquid assets / 30-day net outflows, as disclosed", max_deviation_pp=45.0),
    Indicator("nsfr", "cba.fsr.nsfr", "Net stable funding ratio", "%",
              ("net stable funding ratio", "nsfr"),
              ("xalis sabit maliyyələşdirmə əmsalı",),
              "available / required stable funding, as disclosed"),
    Indicator("npl_ratio", "cba.fsr.npl_ratio", "Non-performing loan ratio (stability report)", "%",
              ("non-performing loan ratio", "npl ratio", "share of non-performing loans"),
              ("qeyri-işlək kreditlərin payı", "qeyri-işlək kreditlərin xüsusi çəkisi"),
              "non-performing loans / gross loans as presented in the stability report; not the overdue-loan ratio",
              max_deviation_pp=4.0),
    Indicator("roa", "cba.fsr.roa", "Return on assets", "%", ("return on assets", "roa"),
              ("aktivlərin gəlirliliyi", "aktivlərin rentabelliyi"), "profit / average assets as disclosed",
              max_deviation_pp=2.0),
    Indicator("roe", "cba.fsr.roe", "Return on equity", "%", ("return on equity", "roe"),
              ("kapitalın gəlirliliyi", "kapitalın rentabelliyi"), "profit / average equity as disclosed",
              max_deviation_pp=12.0),
    Indicator("dollarisation", "cba.fsr.deposit_dollarisation", "Deposit dollarisation (stability report)", "%",
              ("dollarization of deposits", "deposit dollarization", "dollarization level"),
              ("əmanətlərin dollarlaşma səviyyəsi", "dollarlaşma səviyyəsi"),
              "foreign-currency share of deposits as presented in the stability report"),
    Indicator("leverage", "cba.fsr.leverage_ratio", "Leverage ratio", "%", ("leverage ratio",),
              ("leverec əmsalı",), "Tier 1 capital / total exposure as disclosed"),
)

CURRENCY_HINTS = ((("in manat", "manatla", "manat üzrə"), "AZN"), (("in foreign currency", "xarici valyuta"), "FX"))
DISTRIBUTION_HINTS = (("minimum among banks", "min among banks", "banklar üzrə minimum"), ("maximum among banks", "max among banks", "banklar üzrə maksimum"))


SEGMENT_HINTS = ("this segment", "the segment", "of the portfolio", "consumer loan", "mortgage loan", "business loan",
                 "credit card", "sub-sector", "in this sector", "mortgage portfolio", "business portfolio",
                 "issued from the funds", "issued from the resources", "in national currency", "in a foreign currency",
                 "in foreign currency", "national currency", "guarantee fund", "bu seqment", "seqmentind", "portfelind",
                 "ipoteka", "istehlak kredit", "biznes kredit", "kredit kartlar")
# "57% or AZN1.9B of the mortgage portfolio" is a share of a portfolio, not the indicator
SHARE_OF_STOCK = re.compile(r"\d{1,3}(?:\.\d+)?\s*%\s*(?:or|və)\s*(?:AZN|USD|\$)", re.IGNORECASE)
# "reduced by one percentage point" states a movement; only "... by X to Y%" also states a level
CHANGE_NOT_LEVEL = re.compile(r"\bby\b(?!.*\bto\b)", re.IGNORECASE)
REQUIREMENT_BEFORE = re.compile(r"(minimum|requirement|threshold|required|regulatory (?:floor|level)|tələb|normativ)\D{0,25}$", re.IGNORECASE)
# a sentence that sets or discusses an obligation states the rule, not the sector's outcome
# "the requirement is 100%" is a rule; "17.6%, 1.7 times the minimum requirement" is an outcome, so
# the phrase alone is not enough — only wording that sets or conditions an obligation disqualifies.
OBLIGATION = re.compile(r"should maintain|must maintain|are required to|will be applied|is below|are below|"
                        r"starting from|effective from|tələb olunur|minimum requirement (?:is|of|will)|"
                        r"requirement (?:is|of|was) set", re.IGNORECASE)
# other financial sectors publish the same ratio names; only the banking sector belongs in these series
OTHER_SECTOR = re.compile(r"insurance|non-bank credit|microfinanc|securities market|pension fund|sığorta|"
                          r"bank olmayan", re.IGNORECASE)
SCENARIO_WORDS = re.compile(r"stress|adverse|pessimistic|baseline scenario|scenario|ssenari", re.IGNORECASE)
PCT = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")


def _plausible(key: str, value: float) -> bool:
    if key in ("car", "tier1", "leverage", "npl_ratio", "roa", "roe", "dollarisation"):
        return -50.0 <= value <= 100.0
    return 0.0 <= value <= 1000.0          # coverage ratios are quoted well above 100%


def _anchor_hits(sentence: str, language: str) -> list[tuple[int, int, Indicator]]:
    """All indicator anchors in a sentence, in order, longest anchor winning on overlap."""
    low = sentence.lower()
    hits: list[tuple[int, int, Indicator]] = []
    for ind in INDICATORS:
        for anchor in (ind.anchors_en if language == "en" else ind.anchors_az):
            for m in re.finditer(rf"(?<![\w]){re.escape(anchor)}(?![\w])", low):
                hits.append((m.start(), m.end(), ind))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, Indicator]] = []
    for h in hits:
        if kept and h[0] < kept[-1][1]:
            continue
        kept.append(h)
    return kept


def _value_for(sentence: str, anchor_end: int, next_anchor_start: int | None) -> tuple[float, int] | None:
    """The first percentage after the anchor, provided no other indicator intervenes."""
    limit = next_anchor_start if next_anchor_start is not None else len(sentence)
    window = sentence[anchor_end:min(limit, anchor_end + 110)]
    m = PCT.search(window)
    if not m:
        return None
    lead = window[:m.start()]
    if REQUIREMENT_BEFORE.search(lead):
        return None
    if CHANGE_NOT_LEVEL.search(lead):
        return None                       # the sentence gives a movement, not the level
    return float(m.group(1).replace(",", ".")), anchor_end + m.start()


def extract_indicators(passages: list[dict[str, Any]], language: str, *, publication_id: str,
                       reporting_end: dt.date | None, edition_label: str) -> tuple[list[Observation], list[str]]:
    """Indicator levels stated in the report's running text, dated to the report's reporting period.

    Sentence-scoped on purpose. A number is attached to an indicator only when it is the first
    percentage after that indicator's name and no other indicator's name comes between them; the
    one exception is the "A and B were X% and Y%, respectively" form, which is mapped in order only
    when the counts match exactly.
    """
    out: list[Observation] = []
    notes: list[str] = []
    if reporting_end is None:
        return out, ["publication has no established reporting period, so no indicator was extracted from it"]
    seen: set[tuple[str, str]] = set()
    text, spans = flow(passages)
    for offset, raw in sentence_spans(text):
        sentence = denoise(raw)
        low = sentence.lower()
        if SCENARIO_WORDS.search(low):
            continue                              # stress-test statements are handled separately
        if SHARE_OF_STOCK.search(sentence):
            continue                      # a share of a portfolio quoted beside its amount
        if OBLIGATION.search(sentence) or OTHER_SECTOR.search(sentence):
            continue                      # a regulatory requirement, or another sector's ratio
        if len(pcts_all := PCT.findall(sentence)) >= 4 and len(sentence.split()) < 6 * len(pcts_all):
            continue                      # a run of chart axis labels rather than a sentence
        anchors = _anchor_hits(sentence, language)
        if not anchors:
            continue
        pcts = list(PCT.finditer(sentence))
        assignments: list[tuple[Indicator, float, int]] = []
        if "respectively" in low and len(anchors) >= 2 and len(pcts) == len(anchors):
            lead = sentence[:pcts[0].start()]
            if CHANGE_NOT_LEVEL.search(lead[anchors[-1][1]:]):
                continue                  # "increased by X% and Y% respectively" gives movements
            for (a_start, _a_end, ind), m in zip(anchors, pcts):
                assignments.append((ind, float(m.group(1).replace(",", ".")), a_start))
        else:
            for i, (a_start, a_end, ind) in enumerate(anchors):
                nxt = anchors[i + 1][0] if i + 1 < len(anchors) else None
                got = _value_for(sentence, a_end, nxt)
                if got is None:
                    continue
                assignments.append((ind, got[0], a_start))
        for ind, value, a_start in assignments:
            if not _plausible(ind.key, value):
                continue
            if any(h in low for h in SEGMENT_HINTS):
                notes.append(f"{ind.key}: a segment-level figure was not stored as the sector aggregate "
                             f"({cite(passage_at(spans, offset + a_start))})")
                continue
            if any(h in low for hints in DISTRIBUTION_HINTS for h in hints):
                notes.append(f"{ind.key}: a per-bank minimum or maximum was not stored as the sector aggregate "
                             f"({cite(passage_at(spans, offset + a_start))})")
                continue
            dims = {"basis": "regulatory"} if ind.key in ("car", "tier1", "leverage") else {}
            near = low[a_start:a_start + 70]           # "LCR in manat" qualifies; the same words
            for hints, code in CURRENCY_HINTS:        # elsewhere in the sentence do not
                if any(h in near for h in hints):
                    dims["currency"] = code
            key = (ind.series_id, repr(sorted(dims.items())))
            if key in seen:
                continue
            seen.add(key)
            p = passage_at(spans, offset + a_start) or {"page_index": None, "printed_page": None}
            out.append(Observation(
                series_id=ind.series_id, period_end=reporting_end, period_start=None, value=value,
                value_raw=sentence[:220], freq=_freq(edition_label), period_type="period_end_ratio",
                dims=dims, unit=ind.unit, source_id="CBA_STABILITY", population=ind.population, basis=ind.definition,
                label_original=ind.label_en, extraction_method="pdf_text_pattern", publication_id=publication_id,
                passage_id=p.get("passage_id"),
                validation_status="verified" if p.get("validation_status", "verified") == "verified" else "unverified_ocr",
                language=language, cell_ref=cite(p),
            ))
    return out, sorted(set(notes))


def _freq(edition_label: str) -> str:
    return "S" if edition_label.upper().startswith("H") else "A"


# ------------------------------------------------------------------- stress tests

SCENARIOS = {"baseline": ("baseline scenario", "baza ssenari"), "adverse": ("adverse scenario", "pessimistic scenario", "adverse (pessimistic)", "pessimist ssenari", "mənfi ssenari")}
# "... decreases by 2.6 pp in 2026 and by an additional 1.2 pp in 2027 to 14.8% and 13.6%, respectively"
RESPECTIVELY_EN = re.compile(r"in\s+(\d{4})\b.{0,70}?\bin\s+(\d{4})\b.{0,40}?to\s+(\d{1,3}(?:\.\d{1,2})?)\s*%\s*and\s*(\d{1,3}(?:\.\d{1,2})?)\s*%.{0,30}?respectively",
                             re.IGNORECASE | re.DOTALL)
# "... the capital adequacy ratio stands at 19.4% in 2026"
SINGLE_EN = re.compile(r"(?:capital adequacy ratio|car)\s+(?:stands at|stood at|reaches|reached|is|falls to|declines to|decreases to)\s*(\d{1,3}(?:\.\d{1,2})?)\s*%\s*(?:in|for)\s+(\d{4})", re.IGNORECASE)
# "... while a steady increase over the subsequent year results in the ratio reaching 22.5%"
SUBSEQUENT_EN = re.compile(r"subsequent year.{0,80}?reaching\s+(\d{1,3}(?:\.\d{1,2})?)\s*%", re.IGNORECASE | re.DOTALL)


@dataclass
class StressExercise:
    publication_id: str
    scenarios: dict[str, str]                 # scenario -> description passage text
    horizon_years: list[int]
    results: list[Observation]
    passages: list[str]
    note: str


def extract_stress_tests(passages: list[dict[str, Any]], language: str, *, publication_id: str,
                         reporting_end: dt.date | None, edition_label: str) -> tuple[list[Observation], dict[str, Any]]:
    """Stress-test scenarios, horizons and reported capital outcomes.

    Results are stored with `period_type='stress_test_projection'`, the scenario and the exercise's
    starting date, so nothing downstream can present them as an outcome or as a forecast.
    """
    out: list[Observation] = []
    meta: dict[str, Any] = {"scenarios": {}, "horizons": [], "passages": [], "notes": []}
    if reporting_end is None:
        return out, meta
    for p in passages:
        low = p["text"].lower()
        if re.search(r"stress|ssenari|scenario", low):
            meta["passages"].append(p.get("passage_id"))
            for name, hints in SCENARIOS.items():
                if any(h in low for h in hints) and name not in meta["scenarios"]:
                    meta["scenarios"][name] = {"description": p["text"][:700], "passage_id": p.get("passage_id"),
                                               "page": p["page_index"], "printed_page": p.get("printed_page")}
    if language != "en":
        meta["notes"].append("stress-test values are read from the English edition only")
        return out, meta
    text, spans = flow(passages)
    for offset, raw in sentence_spans(text):
        sentence = denoise(raw)
        if not re.search(r"stress|scenario", sentence, re.IGNORECASE):
            continue
        scenario_here = _scenario_at(sentence, len(sentence))
        for m in SINGLE_EN.finditer(sentence):
            value, year = float(m.group(1)), int(m.group(2))
            sc = _scenario_at(sentence, m.start()) or scenario_here
            if sc is None or not (0.0 <= value <= 60.0):
                continue
            out.append(_stress_obs(value, year, sc, publication_id, passage_at(spans, offset + m.start()) or {"page_index": None},
                                   reporting_end, edition_label, m.group(0)))
        for m in SUBSEQUENT_EN.finditer(sentence):
            years = [int(y) for y in re.findall(r"\b(20\d{2})\b", sentence[:m.start()])]
            sc = _scenario_at(sentence, m.start()) or scenario_here
            value = float(m.group(1))
            if sc is None or not years or not (0.0 <= value <= 60.0):
                continue
            out.append(_stress_obs(value, max(years) + 1, sc, publication_id,
                                   passage_at(spans, offset + m.start()) or {"page_index": None}, reporting_end,
                                   edition_label, sentence[:220] + "  [target year read from \'the subsequent year\']"))
        for m in RESPECTIVELY_EN.finditer(sentence):
            y1, y2 = int(m.group(1)), int(m.group(2))
            v1, v2 = float(m.group(3)), float(m.group(4))
            sc = _scenario_at(sentence, m.start()) or scenario_here
            if sc is None or y2 <= y1 or not all(0.0 <= v <= 60.0 for v in (v1, v2)):
                meta["notes"].append("a paired stress statement could not be resolved and was left as evidence only")
                continue
            pg = passage_at(spans, offset + m.start()) or {"page_index": None}
            out.append(_stress_obs(v1, y1, sc, publication_id, pg, reporting_end, edition_label, sentence[:220]))
            out.append(_stress_obs(v2, y2, sc, publication_id, pg, reporting_end, edition_label, sentence[:220]))
    dedup: dict[tuple[str, str, str], Observation] = {}
    for o in out:
        dedup[(o.series_id, o.period_end.isoformat(), o.scenario or "")] = o
    out = list(dedup.values())
    meta["horizons"] = sorted({o.period_end.year for o in out})
    return out, meta


def _scenario_at(text: str, pos: int) -> str | None:
    before = text[:pos].lower()
    best, best_pos = None, -1
    for name, hints in SCENARIOS.items():
        for h in hints:
            i = before.rfind(h)
            if i > best_pos:
                best, best_pos = name, i
    return best


def _stress_obs(value: float, year: int, scenario: str, publication_id: str, passage: dict[str, Any],
                reporting_end: dt.date, edition_label: str, raw: str) -> Observation:
    return Observation(
        series_id="cba.fsr.stress.car", period_end=dt.date(year, 12, 31), period_start=dt.date(year, 1, 1), value=value,
        value_raw=raw[:200], freq="A", period_type="stress_test_projection",
        dims={"scenario": scenario, "exercise": reporting_end.isoformat()}, unit="%", source_id="CBA_STABILITY",
        population="banking sector (CBA top-down stress test)",
        basis=f"stress-test projection of the regulatory capital adequacy ratio under the {scenario} scenario; "
              f"exercise starting from {reporting_end.isoformat()} data. Not a forecast and not an outcome.",
        label_original=f"Stress test CAR, {scenario} scenario", extraction_method="pdf_text_pattern",
        publication_id=publication_id, passage_id=passage.get("passage_id"), validation_status="verified",
        scenario=scenario, horizon_label=f"{year}", language="en",
        cell_ref=f"page {passage['page_index']}" + (f" (printed {passage['printed_page']})" if passage.get("printed_page") else ""),
    )


# --------------------------------------------------------------- prudential changes

REG_STATUS = (
    ("effective", (r"came into (?:force|effect)", r"entered into force", r"in force since", r"applies from", r"qüvvəyə min", r"tətbiq olunur")),
    ("adopted", (r"was approved", r"has been approved", r"adopted", r"the board (?:approved|decided)", r"təsdiq edil", r"qərar qəbul")),
    ("proposed", (r"is planned", r"will be introduced", r"is expected to be introduced", r"draft", r"under consideration",
                  r"nəzərdə tutul", r"planlaşdırıl", r"layihə")),
)
REG_TOPIC = re.compile(r"requirement|regulation|buffer|ratio|limit|rule|prudential|tələb|norma|qayda|bufer", re.IGNORECASE)


def extract_regulatory_changes(passages: list[dict[str, Any]], language: str, *, publication_id: str) -> list[dict[str, Any]]:
    """Prudential measures discussed in the report, separated into proposed, adopted and effective.

    The status is taken from the report's own wording. Nothing here is presented as a requirement in
    force: that needs the underlying official decision, which is recorded as the verification step.
    """
    out: list[dict[str, Any]] = []
    for p in passages:
        text = p["text"]
        if not REG_TOPIC.search(text):
            continue
        low = text.lower()
        status = None
        for name, patterns in REG_STATUS:
            if any(re.search(pat, low) for pat in patterns):
                status = name
                break
        if status is None:
            continue
        out.append({"publication_id": publication_id, "status": status, "language": language,
                    "text": text[:600], "passage_id": p.get("passage_id"), "page": p["page_index"],
                    "printed_page": p.get("printed_page"),
                    "verification": "status as described in the report; confirm against the CBA decision before "
                                    "treating it as a requirement currently in force"})
    return out[:40]
