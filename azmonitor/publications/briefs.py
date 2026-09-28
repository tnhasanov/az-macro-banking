"""Fact packs for the publication briefs.

Three briefs, each triggered by a release rather than by the calendar:

* a Monetary Policy Review brief when a new review appears;
* a Financial Stability Report brief when a new report appears;
* a short decision update when a rate decision is announced.

Each pack names the publication it is about, the one it is compared against, and the monthly
statistics that give the release its banking context. A brief is only produced for a publication
that has actually been extracted, and it states what could not be extracted rather than filling the
gap.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from ..facts import FactPackBuilder
from ..storage.db import Database, utcnow
from ..util.log import get_logger
from .factpack import PUB_LABELS, PublicationFacts

log = get_logger("briefs")

KINDS = {
    "mpr_brief": ("monetary_policy_review", "Monetary Policy Review brief"),
    "fsr_brief": ("financial_stability_report", "Financial Stability Report brief"),
    "decision_update": ("policy_decision", "Monetary policy decision update"),
}


def build_brief(db: Database, kind: str, as_of: dt.date, lang: str = "en",
                publication_id: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown brief kind {kind!r}; expected one of {sorted(KINDS)}")
    pub_type, title = KINDS[kind]
    builder = FactPackBuilder(db, as_of, lang=lang)
    pf = PublicationFacts(db, as_of, lang)
    editions = pf.editions(pub_type)
    if publication_id:
        current = next((p for p in editions if p["publication_id"] == publication_id), None)
        idx = editions.index(current) if current in editions else None
        previous = editions[idx - 1] if idx else None
    else:
        current = editions[-1] if editions else None
        previous = editions[-2] if len(editions) > 1 else None
    if current is None:
        return {"status": "no_publication", "kind": kind, "note": f"no {PUB_LABELS[pub_type]} has been collected"}

    policy = pf.policy_block()
    stability = pf.stability_block()
    pack: dict[str, Any] = {
        "report_type": kind, "title": title, "as_of": as_of.isoformat(), "lang": lang, "generated_at": utcnow(),
        "as_of_definition": "information cutoff at 23:59 Asia/Baku on the stated date",
        "publication": pf._publication_view(current),
        "previous_publication": pf._publication_view(previous),
        "policy": policy, "stability": stability,
        "passages": _key_passages(db, current["publication_id"], pub_type),
        "extraction_notes": _extraction_notes(db, current["publication_id"]),
    }
    pack["context"] = _banking_context(builder, kind)
    pack["metrics"] = builder.metric_refs
    pack["definitions"] = builder.definitions()
    pack["sources"] = [builder._doc_summary(d) for d in builder.sources_used.values()]
    pack["edition"] = {"banking_period": pack["context"].get("banking_period"),
                       "reporting_period_end": (pack["publication"] or {}).get("reporting_period_end"),
                       "published_at": (pack["publication"] or {}).get("published_at")}
    pack["fact_pack_hash"] = hashlib.sha256(
        json.dumps({k: pack[k] for k in ("publication", "policy", "stability", "context")},
                   sort_keys=True, default=str).encode()).hexdigest()[:16]
    pack["status"] = "ok"
    return pack


def _banking_context(builder: FactPackBuilder, kind: str) -> dict[str, Any]:
    """The monthly statistics a reader needs beside the publication's own numbers."""
    refs = [("cba.loans.total_ci.yoy", None, "Loans to the economy, y/y"),
            ("cba.deposits.total.yoy", None, "Total deposits, y/y"),
            ("cba.deposits.fx_share", None, "FX share of deposits"),
            ("cba.ldr", None, "Loan-to-deposit ratio"),
            ("cba.bank.npl.ratio", None, "NPL ratio (monthly prudential table)"),
            ("cba.bank.equity_to_assets", None, "Capital / assets (book)"),
            ("ssc.cpi.all.yoy", None, "CPI inflation, y/y")]
    if kind != "fsr_brief":
        refs += [("cba.rates.new.loan", {"currency": "AZN"}, "Average rate on new AZN loans"),
                 ("cba.rates.new.deposit", {"currency": "AZN"}, "Average rate on new AZN term deposits"),
                 ("cba.rates.new.spread", {"currency": "AZN"}, "Indicative AZN pricing spread"),
                 ("cba.money.m2.yoy", None, "Broad money M2, y/y")]
    out: dict[str, Any] = {"items": []}
    for ref, dims, label in refs:
        key = ref + (("|" + json.dumps(dims, sort_keys=True)) if dims else "")
        snap = builder.snap(ref, dims, key=key)
        if snap.get("available"):
            out["items"].append({"claim_ref": key, "label": label, "snapshot": snap})
    banking = [i["snapshot"]["latest"]["period"] for i in out["items"]
               if i["claim_ref"].startswith("cba.") and (i["snapshot"].get("latest") or {}).get("period")]
    out["banking_period"] = max(banking) if banking else None
    return out


def _key_passages(db: Database, publication_id: str, pub_type: str) -> list[dict[str, Any]]:
    """Quotable passages: the Central Bank's own words, with the page they appear on."""
    wanted = {
        "monetary_policy_review": ("inflyasiya", "inflation", "proqnoz", "forecast", "risk", "kredit", "credit",
                                   "depozit", "deposit", "likvidlik", "liquidity"),
        "policy_decision": ("inflation", "risk", "corridor", "dəhliz", "inflyasiya", "forecast", "proqnoz"),
        "financial_stability_report": ("capital adequacy", "liquidity", "stress", "npl", "non-performing",
                                       "concentration", "dollarization", "profitability", "household", "mortgage"),
    }[pub_type]
    rows = db.passages(publication_id=publication_id, language="en") or db.passages(publication_id=publication_id)
    out = []
    for r in rows:
        text = (r["text"] or "")
        low = text.lower()
        if r["kind"] == "table" or len(text) < 90:
            continue
        hits = [w for w in wanted if w in low]
        if not hits:
            continue
        out.append({"passage_id": r["passage_id"], "text": text[:700], "page_index": r["page_index"],
                    "printed_page": r["printed_page"], "section": r["section"], "language": r["language"],
                    "topics": hits, "cite": f"page {r['page_index']}"
                                            + (f" (printed {r['printed_page']})" if r["printed_page"] else "")})
    out.sort(key=lambda r: (-len(r["topics"]), r["page_index"]))
    return out[:24]


def _extraction_notes(db: Database, publication_id: str) -> dict[str, Any]:
    docs = db.publication_documents(publication_id)
    pages_without_text = 0
    unverified = db.conn.execute(
        "SELECT COUNT(*) FROM passages WHERE publication_id=? AND validation_status<>'verified'",
        (publication_id,)).fetchone()[0]
    return {
        "documents": [{"language": d["language"], "pages": d["page_count"], "version": d["version"],
                       "sha256": (d["sha256"] or "")[:16], "url": d["document_url"]} for d in docs],
        "passages": db.conn.execute("SELECT COUNT(*) FROM passages WHERE publication_id=?", (publication_id,)).fetchone()[0],
        "passages_needing_review": unverified,
        "note": "numbers are extracted from running text and tables only; a figure that appears solely inside a chart "
                "image is not extracted and is left as a passage for a reader to check",
        "pages_without_text": pages_without_text,
    }
