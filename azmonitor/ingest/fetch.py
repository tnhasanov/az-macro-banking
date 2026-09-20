"""HTTP fetching with bounded retries, timeouts, request spacing, byte storage and hashing."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..util.log import get_logger

log = get_logger("fetch")


class FetchError(Exception):
    pass


@dataclass
class Fetched:
    url: str
    final_url: str
    content: bytes
    status: int
    content_type: str | None
    last_modified: str | None
    etag: str | None
    retrieved_at: str
    sha256: str
    from_cache: bool = False


class Fetcher:
    def __init__(self, http_cfg: dict[str, Any], raw_dir: Path, offline: bool = False):
        self.cfg = http_cfg
        self.raw_dir = raw_dir
        self.offline = offline
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": http_cfg.get("user_agent", "az-macro-banking-monitor/0.1")})
        self._last_request_at = 0.0
        self.page_cache: dict[str, str] = {}

    # -- low level ---------------------------------------------------------------
    def _space(self) -> None:
        gap = float(self.cfg.get("min_interval_seconds", 1.0))
        wait = self._last_request_at + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, *, allow_html: bool = True, max_mb: float | None = None) -> Fetched:
        if self.offline:
            raise FetchError(f"offline mode: {url}")
        retries = int(self.cfg.get("retries", 3))
        backoff = float(self.cfg.get("backoff_seconds", 2))
        timeout = float(self.cfg.get("timeout_seconds", 90))
        max_bytes = int((max_mb or float(self.cfg.get("max_download_mb", 60))) * 1024 * 1024)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self._space()
                self._last_request_at = time.monotonic()
                r = self.session.get(url, timeout=timeout, stream=True)
                if r.status_code >= 500 or r.status_code in (408, 429):
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                if r.status_code != 200:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                chunks = []
                size = 0
                for chunk in r.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"download exceeds {max_bytes} bytes: {url}")
                content = b"".join(chunks)
                ctype = r.headers.get("Content-Type")
                if not allow_html and ctype and "text/html" in ctype.lower():
                    raise FetchError(f"expected a file but received HTML (soft 404?) for {url}")
                return Fetched(
                    url=url, final_url=r.url, content=content, status=r.status_code, content_type=ctype,
                    last_modified=r.headers.get("Last-Modified"), etag=r.headers.get("ETag"),
                    retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            except (requests.RequestException, FetchError) as exc:  # bounded retry
                last_exc = exc
                if attempt < retries and not (isinstance(exc, FetchError) and "expected a file" in str(exc)):
                    sleep = backoff * (2 ** attempt)
                    log.warning("fetch failed (%s); retry %d/%d in %.0fs", exc, attempt + 1, retries, sleep)
                    time.sleep(sleep)
                    continue
                break
        raise FetchError(f"giving up on {url}: {last_exc}")

    def get_text(self, url: str) -> str:
        if url in self.page_cache:
            return self.page_cache[url]
        f = self.get(url)
        enc = "utf-8"
        text = f.content.decode(enc, errors="replace")
        self.page_cache[url] = text
        return text

    # -- storage -----------------------------------------------------------------
    @staticmethod
    def extension_for(url: str, content_type: str | None) -> str:
        path = urlparse(url).path
        m = re.search(r"\.([A-Za-z0-9]{2,5})$", path)
        if m:
            return m.group(1).lower()
        if content_type:
            ct = content_type.lower()
            if "pdf" in ct:
                return "pdf"
            if "spreadsheetml" in ct:
                return "xlsx"
            if "ms-excel" in ct:
                return "xls"
            if "html" in ct:
                return "html"
        return "bin"

    def store(self, dataset_id: str, fetched: Fetched) -> Path:
        ext = self.extension_for(fetched.final_url, fetched.content_type)
        d = self.raw_dir / dataset_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{fetched.sha256[:16]}.{ext}"
        if not p.exists():
            tmp = p.with_suffix(p.suffix + ".part")
            with open(tmp, "wb") as fh:
                fh.write(fetched.content)
            os.replace(tmp, p)
        return p
