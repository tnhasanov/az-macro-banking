"""Text, table and provenance extraction from publication files.

Every passage keeps the physical page index *and* the page number printed on the page, because the
CBA reports carry cover and divider pages, so the two differ by one or two throughout a report and
a citation that names only one of them cannot be checked by a reader.

Extraction order follows the brief: native text first, tables from the PDF's own ruling lines, OCR
only where a page carries no text layer. A page that needed OCR, and every number read from one,
is marked `unverified_ocr` and is not admitted to the fact pack until a check confirms it. Numbers
that exist only inside a chart image are never extracted at all.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ..util.log import get_logger

log = get_logger("pub_extract")

MIN_TEXT_CHARS = 120          # a page with less text than this is treated as an image page
MIN_PASSAGE_CHARS = 40
PAGE_NUM_RE = re.compile(r"(?:^|\s)(\d{1,3})\s*$")
SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([^\d].{2,90})$")
ALLCAPS_RE = re.compile(r"^[^a-zçəğıöşü]{6,90}$")


@dataclass
class Passage:
    ord: int
    page_index: int
    printed_page: str | None
    section: str | None
    kind: str                      # text | table | heading
    text: str
    extraction_method: str         # pdf_text | pdf_table | ocr | html
    validation_status: str         # verified | unverified_ocr | needs_ocr

    def passage_id(self, doc_id: str) -> str:
        h = hashlib.sha256(f"{doc_id}|{self.ord}|{self.text[:200]}".encode("utf-8")).hexdigest()[:12]
        return f"{doc_id}#p{self.ord}:{h}"


@dataclass
class ExtractResult:
    passages: list[Passage] = field(default_factory=list)
    page_count: int = 0
    pages_without_text: list[int] = field(default_factory=list)
    ocr_used: bool = False
    method: str = "pdf_text"
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def text(self, max_chars: int | None = None) -> str:
        t = "\n\n".join(p.text for p in self.passages)
        return t[:max_chars] if max_chars else t


# --------------------------------------------------------------------------- PDF

def _gutter(words, width: float) -> float | None:
    """Find the vertical white gutter between text columns.

    A fixed split at the page midpoint cuts words in half on pages where a chart shifts the body
    text, so the split is taken from the widest empty vertical band in the middle of the page.
    """
    if len(words) < 40:
        return None
    step = 2.0
    bins = int(width / step) + 1
    filled = [False] * bins
    for w in words:
        for b in range(max(0, int(w["x0"] / step)), min(bins - 1, int(w["x1"] / step)) + 1):
            filled[b] = True
    lo, hi = int(0.33 * width / step), int(0.67 * width / step)
    best, run_start, best_len = None, None, 0
    for b in range(lo, hi + 1):
        if not filled[b]:
            run_start = b if run_start is None else run_start
            if (b - run_start + 1) > best_len:
                best_len, best = b - run_start + 1, (run_start, b)
        else:
            run_start = None
    if best is None or best_len * step < 10:
        return None
    centre = (best[0] + best[1] + 1) / 2 * step
    left = sum(1 for w in words if w["x1"] <= centre)
    right = sum(1 for w in words if w["x0"] >= centre)
    if left < 0.15 * len(words) or right < 0.15 * len(words):
        return None
    return centre


def _columns(page) -> tuple[list[str], str]:
    """Body text of a page, split into reading-order columns, with headers and footers removed."""
    top, bottom = 0.055 * page.height, 0.945 * page.height
    try:
        body = page.crop((0, top, page.width, bottom))
        words = body.extract_words()
    except Exception:
        return [page.extract_text() or ""], ""
    footer = ""
    try:
        footer = page.crop((0, bottom, page.width, page.height)).extract_text() or ""
    except Exception:
        pass
    gutter = _gutter(words, page.width)
    if gutter is None:
        return [body.extract_text() or ""], footer
    out = []
    for box in ((0, top, gutter, bottom), (gutter, top, page.width, bottom)):
        try:
            out.append(body.crop(box).extract_text() or "")
        except Exception:
            out.append("")
    return out, footer


def _printed_page(footer_text: str) -> str | None:
    """The page number as printed on the page, read from the footer band."""
    for ln in reversed([l.strip() for l in (footer_text or "").split("\n") if l.strip()]):
        m = PAGE_NUM_RE.search(ln)
        if m and len(ln) <= 120:
            return m.group(1)
    return None


def _blocks(text: str) -> list[str]:
    """Paragraph blocks: blank-line separated, wrapped lines joined."""
    out, cur = [], []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        out.append(" ".join(cur))
    return [re.sub(r"\s{2,}", " ", b).strip() for b in out if b.strip()]


def _ocr_page(path: Path, page_index: int) -> str | None:
    """OCR one page when the PDF has no text layer. Requires tesseract; absent, the page is
    reported as needing OCR rather than silently dropped."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return None
    try:
        with subprocess.Popen(["pdftoppm", "-f", str(page_index), "-l", str(page_index), "-r", "200", "-png", str(path), "-singlefile", "-"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            png, _ = proc.communicate(timeout=120)
        if not png:
            return None
        res = subprocess.run(["tesseract", "stdin", "stdout", "-l", "aze+eng"], input=png, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=180)
        return res.stdout.decode("utf-8", "replace")
    except Exception as exc:  # OCR is best-effort; failure never aborts extraction
        log.warning("OCR failed on page %d: %s", page_index, exc)
        return None


def _running_lines(pages: list[list[str]]) -> set[str]:
    """Lines repeated across many pages are the running header and footer, not content."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for cols in pages:
        seen = set()
        for col in cols:
            for ln in col.split("\n"):
                t = re.sub(r"\s+", " ", ln).strip()
                if t and len(t) < 120:
                    seen.add(t)
        counts.update(seen)
    threshold = max(3, int(0.2 * len(pages)))
    return {t for t, n in counts.items() if n >= threshold}


def extract_pdf(path: Path, *, with_tables: bool = True, max_pages: int = 400) -> ExtractResult:
    import pdfplumber

    res = ExtractResult()
    per_page: list[dict[str, Any]] = []
    with pdfplumber.open(str(path)) as pdf:
        res.page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages[:max_pages], start=1):
            try:
                cols, footer = _columns(page)
            except Exception as exc:
                res.warnings.append(f"page {i}: {type(exc).__name__}: {exc}")
                continue
            page_text = "\n".join(c for c in cols if c)
            method, status = "pdf_text", "verified"
            if len(page_text.strip()) < MIN_TEXT_CHARS:
                res.pages_without_text.append(i)
                ocr = _ocr_page(path, i)
                if ocr and len(ocr.strip()) >= MIN_TEXT_CHARS:
                    cols, method, status = [ocr], "ocr", "unverified_ocr"
                    res.ocr_used = True
                else:
                    status, method = "needs_ocr", "none"
            tables = []
            if with_tables and status != "needs_ocr":
                try:
                    tables = page.extract_tables()
                except Exception:
                    tables = []
            per_page.append({"index": i, "cols": cols, "printed": _printed_page(footer), "method": method,
                             "status": status, "tables": tables})
    running = _running_lines([p["cols"] for p in per_page])
    ordinal = 0
    section: str | None = None
    for pg in per_page:
        for col in pg["cols"]:
            kept = "\n".join("" if re.sub(r"\s+", " ", ln).strip() in running else ln for ln in (col or "").split("\n"))
            for block in _blocks(kept):
                kind = "text"
                m = SECTION_RE.match(block)
                if m and len(block) < 110:
                    section, kind = f"{m.group(1)} {m.group(2).strip()}", "heading"
                elif ALLCAPS_RE.match(block) and len(block) < 90:
                    section, kind = block.title(), "heading"
                if kind == "text" and len(block) < MIN_PASSAGE_CHARS:
                    continue
                ordinal += 1
                res.passages.append(Passage(ordinal, pg["index"], pg["printed"], section, kind, block, pg["method"], pg["status"]))
        for t in pg["tables"]:
            rows = [" | ".join((c or "").replace("\n", " ").strip() for c in row) for row in t if any(row)]
            body = "\n".join(r for r in rows if r.strip(" |"))
            if len(body) < MIN_PASSAGE_CHARS:
                continue
            ordinal += 1
            res.passages.append(Passage(ordinal, pg["index"], pg["printed"], section, "table", body, "pdf_table", pg["status"]))
    if res.pages_without_text and not res.ocr_used:
        res.warnings.append(f"{len(res.pages_without_text)} page(s) have no text layer and OCR is not installed; "
                            "their content is not available for citation")
    res.method = "ocr" if res.ocr_used else "pdf_text"
    return res


# -------------------------------------------------------------------------- HTML

def extract_press_release(path: Path) -> ExtractResult:
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".single-news") or soup.select_one(".content") or soup.select_one("article") or soup.body
    res = ExtractResult(page_count=1, method="html")
    if node is None:
        res.warnings.append("press release body not found")
        return res
    title_el = node.find(["h1", "h2"])
    title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
    ordinal = 0
    if title:
        ordinal += 1
        res.passages.append(Passage(ordinal, 1, None, None, "heading", title, "html", "verified"))
    for el in node.find_all(["p", "li", "td"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if len(txt) < MIN_PASSAGE_CHARS:
            continue
        if txt in (p.text for p in res.passages):
            continue
        ordinal += 1
        res.passages.append(Passage(ordinal, 1, None, None, "text", txt, "html", "verified"))
    if not res.passages:
        body = " ".join(node.get_text(" ", strip=True).split())
        if body:
            res.passages.append(Passage(1, 1, None, None, "text", body, "html", "verified"))
    res.meta["title"] = title
    res.meta["attachments"] = [a.get("href") for a in node.select("a.download_item[href]")]
    return res


def extract(path: Path, extension: str | None, *, with_tables: bool = True) -> ExtractResult:
    if (extension or "").lower() == "html":
        return extract_press_release(path)
    return extract_pdf(path, with_tables=with_tables)
