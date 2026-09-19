"""Sentence-level reading of extracted passages.

Publication PDFs interrupt paragraphs with charts, so a single sentence is often split across two
passages and salted with axis labels. Matching runs over a joined flow of the document's prose with
a span index back to the passage each character came from, which keeps sentences intact while every
extracted number can still be attributed to the page it was printed on.
"""
from __future__ import annotations

import re
from typing import Any

# a bare number not attached to a unit word is a chart axis label, not part of the sentence
NOISE_TOKEN = re.compile(
    r"(?<!\S)"
    r"(?!(?:19|20)\d{2}(?![\d.,]))"          # a bare four-digit year is content, not an axis label
    r"-?\d+(?:[.,]\d+)?(?!\S)"
    r"(?!\s+(?:ay|ayda|ay[ıi]n|ay[ıi]nda|il|ild[əe]|ilin|month|months|year|years|f\.b\.|faiz|pp|p\.p\.|times|d[əe]f[əe]))",
    re.IGNORECASE)


def denoise(text: str) -> str:
    """Drop chart axis labels printed inside a paragraph, keeping numbers that carry a unit."""
    return re.sub(r"\s{2,}", " ", NOISE_TOKEN.sub(" ", text)).strip()


def flow(passages: list[dict[str, Any]], include_tables: bool = False) -> tuple[str, list[tuple[int, int, dict[str, Any]]]]:
    parts: list[str] = []
    spans: list[tuple[int, int, dict[str, Any]]] = []
    pos = 0
    for p in passages:
        if p["kind"] == "table" and not include_tables:
            continue
        t = p["text"]
        spans.append((pos, pos + len(t), p))
        parts.append(t)
        pos += len(t) + 1
    return " ".join(parts), spans


def passage_at(spans: list[tuple[int, int, dict[str, Any]]], idx: int) -> dict[str, Any] | None:
    for lo, hi, p in spans:
        if lo <= idx < hi:
            return p
    return spans[-1][2] if spans else None


def sentence_spans(text: str) -> list[tuple[int, str]]:
    """(offset, sentence) pairs. The split needs whitespace after the stop, so "17.6%" stays whole."""
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+", text):
        out.append((start, text[start:m.start()]))
        start = m.end()
    if start < len(text):
        out.append((start, text[start:]))
    return [(i, s.strip()) for i, s in out if s.strip()]


def cite(passage: dict[str, Any] | None) -> str:
    if not passage:
        return ""
    printed = passage.get("printed_page")
    return f"page {passage['page_index']}" + (f" (printed {printed})" if printed else "")
