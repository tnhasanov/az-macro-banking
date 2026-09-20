"""Discovery of CBA narrative publications.

Both language versions of each entry page are read (`?language=az` / `?language=en`) and every
document link found on them is resolved to an edition. Nothing is derived from file names: the
CBA stores its assets under opaque hashes and, for a few files, under hand-written names, so the
link text is the only stable description.

Policy decisions are press releases rather than attachments, so they are discovered from the
decision page's links to `/press-release-<id>/...`; that numeric id is shared by the Azerbaijani
and English versions and is what joins them into a single publication.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup

from ..discovery import DiscoveredDocument, _ext
from ..ingest.fetch import FetchError, Fetcher
from ..util.log import get_logger
from .editions import classify_title, decision_edition, publication_id

log = get_logger("pub_discovery")

PRESS_RE = re.compile(r"/press-release-(\d+)/")
DATE_IN_TITLE = re.compile(r"\((\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\)")


def with_language(url: str, lang: str) -> str:
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "language"]
    q.append(("language", lang))
    return urlunparse(parts._replace(query=urlencode(q)))


def _title_date(title: str) -> dt.date | None:
    m = DATE_IN_TITLE.search(title)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def discover_cba_publications(source_id: str, scfg: dict[str, Any], fetcher: Fetcher,
                              history_start: str | None = None) -> list[DiscoveredDocument]:
    """Attachment-based publications: monetary policy reviews, policy directions, stability reports."""
    out: list[DiscoveredDocument] = []
    start_year = int((history_start or "2020-01")[:4])
    for ds in scfg.get("datasets", []) or []:
        pub_type = ds["pub_type"]
        for lang, page_url in (ds.get("pages") or {}).items():
            try:
                html = fetcher.get_text(page_url)
            except FetchError as exc:
                log.warning("%s (%s) page failed: %s", ds["id"], lang, exc)
                continue
            soup = BeautifulSoup(html, "html.parser")
            n = 0
            for a in soup.select("a.download_item"):
                href = a.get("href")
                if not href:
                    continue
                p = a.find("p")
                title = " ".join((p.get_text(" ", strip=True) if p else a.get_text(" ", strip=True)).split())
                span = a.find("span")
                size_text = span.get_text(strip=True) if span else None
                info = classify_title(pub_type, title)
                if info is None:
                    log.debug("%s: unclassified title %r", ds["id"], title[:80])
                    continue
                year = int(info.edition_key[:4])
                if year < start_year:
                    continue
                out.append(DiscoveredDocument(
                    source_id=source_id, dataset_id=ds["id"], discovery_url=page_url, document_url=urljoin(page_url, href),
                    title=title, size_text=size_text, published_at=None, published_at_basis=None, extension=_ext(href),
                    extra={"pub_type": pub_type, "language": lang, "edition_key": info.edition_key, "edition_label": info.label_en,
                           "reporting_start": info.reporting_start.isoformat() if info.reporting_start else None,
                           "reporting_end": info.reporting_end.isoformat() if info.reporting_end else None,
                           "reporting_frequency": info.frequency, "period_basis": info.period_basis,
                           "forward_looking": info.forward_looking,
                           "publication_id": publication_id(pub_type, info.edition_key)},
                ))
                n += 1
            log.info("%s (%s): %d editions from %s", ds["id"], lang, n, page_url)
    return out


def discover_cba_decisions(source_id: str, scfg: dict[str, Any], fetcher: Fetcher,
                           history_start: str | None = None) -> list[DiscoveredDocument]:
    """Monetary policy decisions: one press release per decision, in both languages."""
    ds = next((d for d in (scfg.get("datasets") or []) if d.get("pub_type") == "policy_decision"), None)
    if ds is None:
        return []
    start_year = int((history_start or "2020-01")[:4])
    found: dict[str, dict[str, Any]] = {}
    for lang, page_url in (ds.get("pages") or {}).items():
        try:
            html = fetcher.get_text(page_url)
        except FetchError as exc:
            log.warning("decision page (%s) failed: %s", lang, exc)
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = PRESS_RE.search(a["href"])
            if not m:
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            announced = _title_date(title)
            rec = found.setdefault(m.group(1), {"announced": None, "titles": {}, "url": urljoin(page_url, a["href"].split("?")[0])})
            if announced and not rec["announced"]:
                rec["announced"] = announced
            if title:
                rec["titles"].setdefault(lang, title)
    out: list[DiscoveredDocument] = []
    for pid, rec in found.items():
        announced = rec["announced"]
        if announced is None or announced.year < start_year:
            continue
        info = decision_edition(announced)
        pub_id = publication_id("policy_decision", pid)
        for lang, page_url in (ds.get("pages") or {}).items():
            out.append(DiscoveredDocument(
                source_id=source_id, dataset_id=ds["id"], discovery_url=page_url, document_url=with_language(rec["url"], lang),
                title=rec["titles"].get(lang) or rec["titles"].get("az") or f"CBA decision {announced.isoformat()}",
                published_at=announced, published_at_basis="announcement date in the decision list", extension="html",
                extra={"pub_type": "policy_decision", "language": lang, "edition_key": info.edition_key, "edition_label": info.label_en,
                       "reporting_start": announced.isoformat(), "reporting_end": announced.isoformat(),
                       "reporting_frequency": "event", "period_basis": "announcement date",
                       "announcement_date": announced.isoformat(), "press_release_id": pid, "publication_id": pub_id},
            ))
    log.info("policy decisions: %d releases (%d documents) from %s onwards", len(out) // max(1, len(ds.get("pages") or {1: 1})), len(out), start_year)
    return out
