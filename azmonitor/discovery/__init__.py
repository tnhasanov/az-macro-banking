"""Source discovery: resolve current official document links from the configured entry pages."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..ingest.fetch import Fetcher, FetchError
from ..util.log import get_logger
from ..util.periods import parse_ddmmyyyy

log = get_logger("discovery")


@dataclass
class DiscoveredDocument:
    source_id: str
    dataset_id: str | None
    discovery_url: str
    document_url: str
    title: str
    size_text: str | None = None
    published_at: dt.date | None = None
    published_at_basis: str | None = None
    extension: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _ext(url: str) -> str | None:
    m = re.search(r"\.([A-Za-z0-9]{2,5})(?:\?|$)", url)
    return m.group(1).lower() if m else None


# --- CBA -----------------------------------------------------------------------

def discover_cba(source_id: str, scfg: dict[str, Any], fetcher: Fetcher) -> list[DiscoveredDocument]:
    """CBA pages list files as <a id="asset-N" class="download_item" href=...><p>Title (dd.mm.yyyy)</p><span>size</span></a>.
    In-text anchors "#asset-N" are placeholders resolved against those elements."""
    url = scfg["entry_point"]
    html = fetcher.get_text(url)
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("a.download_item")
    out: list[DiscoveredDocument] = []
    for a in items:
        href = a.get("href")
        if not href:
            continue
        p = a.find("p")
        title = " ".join((p.get_text(" ", strip=True) if p else a.get_text(" ", strip=True)).split())
        span = a.find("span")
        size_text = span.get_text(strip=True) if span else None
        pub = parse_ddmmyyyy(title)
        doc = DiscoveredDocument(
            source_id=source_id, dataset_id=None, discovery_url=url, document_url=urljoin(url, href), title=title,
            size_text=size_text, published_at=pub, published_at_basis="title_date" if pub else None, extension=_ext(href),
            extra={"asset_id": a.get("id")},
        )
        # year grouping from surrounding <dt>/<strong> headings when present
        out.append(doc)
    # assign datasets by title pattern
    for ds in scfg.get("datasets", []) or []:
        pat = re.compile(ds["title_pattern"], re.IGNORECASE)
        only_ext = ds.get("only_extension")
        for d in out:
            if d.dataset_id is None and pat.search(d.title) and (only_ext is None or d.extension == only_ext):
                d.dataset_id = ds["id"]
    log.info("%s: %d download items, %d matched to datasets", source_id, len(out), sum(1 for d in out if d.dataset_id))
    return out


# --- SSC -----------------------------------------------------------------------

def _edition_period(title: str):
    from .. import util  # noqa: F401
    from ..util.periods import az_month_number, month_end
    m = re.search(r"(\d{4})-c[iıuü] ilin\s+yanvar-([a-zəığöşüçİ]+)\s+ay", title, re.IGNORECASE)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return month_end(int(m.group(1)), mon)
    m = re.search(r"(\d{4})-c[iıuü] ilin\s+([a-zəığöşüçİ]+)\s+ay", title, re.IGNORECASE)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return month_end(int(m.group(1)), mon)
    return None


def discover_ssc_macro(source_id: str, scfg: dict[str, Any], fetcher: Fetcher, max_pages: int = 120,
                       history_start: str | None = None) -> list[DiscoveredDocument]:
    """Walk the SSC macroeconomy news pages: one page per monthly edition, newest first.
    Each page carries the headline HTML table (collected as a document itself), the report PDF link
    and the page publication date. Walking stops when an edition is older than history_start."""
    base = scfg["entry_point"]
    out: list[DiscoveredDocument] = []
    seen: set[str] = set()
    stop_year = int(history_start[:4]) if history_start else None
    for page in range(1, max_pages + 1):
        url = re.sub(r"page=\d+", f"page={page}", base) if "page=" in base else f"{base}?page={page}"
        try:
            html = fetcher.get_text(url)
        except FetchError as exc:
            log.warning("SSC macro page %d failed: %s", page, exc)
            break
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1.page-title")
        if h1 is None:
            break
        title = " ".join(h1.get_text(" ", strip=True).replace("Ətraflı", "").split())
        period = _edition_period(title)
        meta = h1.find_next("div", class_="news-meta")
        pub = parse_ddmmyyyy(meta.get_text(" ", strip=True)) if meta else None
        table = soup.select_one("#macro_iq") or soup.select_one("#tmacro")
        if table is not None and url not in seen:
            seen.add(url)
            out.append(DiscoveredDocument(source_id=source_id, dataset_id="ssc_macro_headline_html", discovery_url=url, document_url=url,
                                          title=title, published_at=pub, published_at_basis="page_news_date" if pub else None, extension="html",
                                          extra={"edition_period": period.isoformat() if period else None, "page": page}))
        a = h1.find("a", href=True)
        if a and re.search(r"(hesabat|doklad)_\d{4}-\d{2}\.pdf", a["href"]):
            doc_url = urljoin(url, a["href"])
            if doc_url not in seen:
                seen.add(doc_url)
                out.append(DiscoveredDocument(source_id=source_id, dataset_id="ssc_monthly_report", discovery_url=url, document_url=doc_url,
                                              title=title, published_at=pub, published_at_basis="page_news_date" if pub else None, extension="pdf",
                                              extra={"edition_period": period.isoformat() if period else None, "page": page}))
        if page == 1:
            for a2 in soup.select("a[href*='news/index.php']"):
                t = " ".join(a2.get_text(" ", strip=True).split())
                href = urljoin(url, a2["href"])
                if t and href not in seen and re.search(r"\d{4}", t):
                    seen.add(href)
                    out.append(DiscoveredDocument(source_id=source_id, dataset_id=None, discovery_url=url, document_url=href, title=t,
                                                  extension="html", extra={"kind": "release_headline"}))
        if stop_year and period and period.year < stop_year:
            break
    log.info("%s: %d edition pages, %d report PDFs discovered", source_id,
             sum(1 for d in out if d.dataset_id == "ssc_macro_headline_html"), sum(1 for d in out if d.dataset_id == "ssc_monthly_report"))
    return out


def discover_ssc_topics(source_id: str, scfg: dict[str, Any], fetcher: Fetcher) -> list[DiscoveredDocument]:
    out: list[DiscoveredDocument] = []
    pages = scfg.get("topic_pages", {})
    for topic, url in pages.items():
        try:
            html = fetcher.get_text(url)
        except FetchError as exc:
            log.warning("SSC topic page %s failed: %s", topic, exc)
            continue
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.search(r"\.(xls|xlsx|pdf|csv|zip)(\?|$)", href, re.IGNORECASE):
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            links.append((title, urljoin(url, href)))
        for ds in scfg.get("datasets", []) or []:
            if ds.get("topic") != topic:
                continue
            pat = re.compile(ds["href_pattern"], re.IGNORECASE)
            for title, full in links:
                if pat.search(full):
                    out.append(DiscoveredDocument(source_id=source_id, dataset_id=ds["id"], discovery_url=url, document_url=full, title=title,
                                                  extension=_ext(full), extra={"topic": topic}))
    log.info("%s: %d topic files matched", source_id, len(out))
    return out


def discover(source_id: str, scfg: dict[str, Any], fetcher: Fetcher, history_start: str | None = None) -> list[DiscoveredDocument]:
    method = scfg.get("discovery", "none")
    if method in ("cba_publications", "cba_decisions"):
        # imported here: the publications package builds on the types defined above
        from ..publications.discovery import discover_cba_decisions, discover_cba_publications

        fn = discover_cba_publications if method == "cba_publications" else discover_cba_decisions
        return fn(source_id, scfg, fetcher, history_start=history_start)
    if method == "cba_download_items":
        return discover_cba(source_id, scfg, fetcher)
    if method == "ssc_macro_news":
        return discover_ssc_macro(source_id, scfg, fetcher, history_start=history_start)
    if method == "ssc_topic_pages":
        return discover_ssc_topics(source_id, scfg, fetcher)
    return []
