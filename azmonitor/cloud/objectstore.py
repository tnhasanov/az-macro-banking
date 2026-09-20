"""The dataset and the report archive, kept in object storage between runs.

A worker starts with an empty disk and ends by throwing it away, so the working directory has to be
pulled down at the start of a run and pushed back at the end. The engine already brackets its whole
cycle with restore and save hooks for exactly this, inside the job lock, so nothing about the
pipeline changes.

Two things are stored differently, on purpose:

**The dataset** is one object, replaced wholesale. It is a SQLite file and its raw documents, and it
only makes sense as a set - half a dataset is not a smaller dataset, it is a broken one. It is
uploaded to a versioned key and the pointer is moved only after the upload is verified, so a run
that dies mid-upload leaves the previous dataset current.

**Reports** are many objects, written once and never rewritten. An edition version directory is
immutable in the engine, so it is immutable here: a key that already exists is not overwritten, and
an attempt to do so is a bug worth failing on rather than a silent replacement.

The backend is chosen by URL scheme, so the same code runs against Vercel Blob in production and a
local directory in a test. Nothing here knows about the dashboard, and the dashboard never writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from ..util.log import get_logger

log = get_logger("cloud.objectstore")

# The dataset is pushed as one tarball; anything under these names is what a run needs to resume.
DATASET_PARTS = ("monitor.sqlite", "deliveries.sqlite", "raw", "state")
POINTER_KEY = "dataset/current.json"
CHUNK = 1024 * 1024


class StorageError(RuntimeError):
    """Something went wrong talking to the store. Never raised for "the object is not there"."""


class ObjectStore:
    """The operations the worker needs, and no more."""

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream",
            overwrite: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def list(self, prefix: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def url_for(self, key: str) -> str | None:
        """A URL the dashboard can proxy. None when the backend has no addressable URL."""
        return None


class LocalObjectStore(ObjectStore):
    """A directory pretending to be a bucket.

    Not a mock: it is the fallback the engine uses when no cloud store is configured, and it is what
    the tests run against, so the code path exercised in a test is the code path that runs in
    production apart from the transport.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # a key is a path, and a key that escapes the root is a bug or an attack
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise StorageError(f"key escapes the store root: {key!r}")
        return p

    def put(self, key, data, *, content_type="application/octet-stream", overwrite=False):
        p = self._path(key)
        if p.exists() and not overwrite:
            raise StorageError(f"{key} already exists and this store does not overwrite by default")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def get(self, key):
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def exists(self, key):
        return self._path(key).exists()

    def list(self, prefix):
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        out = []
        for f in sorted(base.rglob("*")):
            if f.is_file():
                out.append({"key": str(f.relative_to(self.root)), "size": f.stat().st_size,
                            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f.stat().st_mtime))})
        return out

    def url_for(self, key):
        return None


class VercelBlobStore(ObjectStore):
    """Vercel Blob over its REST API.

    Uploads go to `blob.vercel-storage.com` with the read-write token; the token never appears in a
    URL and never leaves this process. Blobs are created with `addRandomSuffix=false` so a key is
    stable and an edition's files can be found by path rather than by remembering a generated name.

    Access is deliberately not public: the dashboard reads through its own authenticated route and
    streams the bytes, so a report URL cannot be forwarded to someone who is not signed in.
    """

    API = "https://blob.vercel-storage.com"

    def __init__(self, token: str | None = None, prefix: str = ""):
        self.token = token or os.environ.get("BLOB_READ_WRITE_TOKEN")
        if not self.token:
            raise StorageError("BLOB_READ_WRITE_TOKEN is not set; Vercel Blob cannot be used")
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}".lstrip("/") if self.prefix else key

    def _request(self, method: str, url: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 120) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"authorization": f"Bearer {self.token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, b""
            body = b""
            try:
                body = exc.read()[:500]
            except Exception:
                pass
            raise StorageError(f"blob {method} {url} failed: HTTP {exc.code} {body.decode('utf-8', 'replace')}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StorageError(f"blob {method} {url} failed: {type(exc).__name__}: {exc}") from exc

    def put(self, key, data, *, content_type="application/octet-stream", overwrite=False):
        full = self._key(key)
        if not overwrite and self.exists(key):
            raise StorageError(f"{full} already exists; published editions are immutable")
        status, body = self._request(
            "PUT", f"{self.API}/{full}", data=data,
            headers={"content-type": content_type, "x-content-type": content_type,
                     "x-add-random-suffix": "0", "x-allow-overwrite": "1" if overwrite else "0",
                     "x-api-version": "7", "x-cache-control-max-age": "31536000"})
        if status not in (200, 201):
            raise StorageError(f"blob put {full} returned {status}")
        meta = json.loads(body or b"{}")
        return {"key": full, "size": len(data), "url": meta.get("url"),
                "sha256": hashlib.sha256(data).hexdigest()}

    def get(self, key):
        listing = self.list(self._key(key))
        hit = next((b for b in listing if b["key"] == self._key(key)), None)
        if not hit or not hit.get("url"):
            return None
        # the download URL carries no credential, so it is fetched plainly
        try:
            with urllib.request.urlopen(hit["url"], timeout=300) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise StorageError(f"blob download {key} failed: {type(exc).__name__}: {exc}") from exc

    def exists(self, key):
        full = self._key(key)
        return any(b["key"] == full for b in self.list(full))

    def list(self, prefix):
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            url = f"{self.API}?prefix={urllib.parse.quote(prefix)}&limit=1000"
            if cursor:
                url += f"&cursor={urllib.parse.quote(cursor)}"
            status, body = self._request("GET", url)
            if status == 404:
                return out
            payload = json.loads(body or b"{}")
            for b in payload.get("blobs", []):
                out.append({"key": b.get("pathname"), "size": b.get("size"),
                            "uploaded_at": b.get("uploadedAt"), "url": b.get("url")})
            cursor = payload.get("cursor")
            if not payload.get("hasMore") or not cursor:
                return out

    def url_for(self, key):
        hit = next((b for b in self.list(self._key(key)) if b["key"] == self._key(key)), None)
        return hit.get("url") if hit else None


def store_from_env() -> ObjectStore:
    """The store this deployment uses, chosen by what is configured.

    Order matters: an explicitly configured local directory wins, so a test or a local run never
    reaches for the network by accident just because a token happens to be in the environment.
    """
    local = os.environ.get("AZMONITOR_OBJECT_STORE_DIR")
    if local:
        return LocalObjectStore(local)
    if os.environ.get("BLOB_READ_WRITE_TOKEN"):
        return VercelBlobStore(prefix=os.environ.get("AZMONITOR_BLOB_PREFIX", ""))
    raise StorageError(
        "no object store is configured: set AZMONITOR_OBJECT_STORE_DIR for a local directory or "
        "BLOB_READ_WRITE_TOKEN for Vercel Blob")


# ------------------------------------------------------------------- the dataset

def _tar(paths: Iterable[Path], base: Path, dest: Path) -> dict[str, Any]:
    n = 0
    with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
        for p in paths:
            if not p.exists():
                continue
            tf.add(p, arcname=str(p.relative_to(base)))
            n += 1 if p.is_file() else sum(1 for _ in p.rglob("*") if _.is_file())
    return {"files": n, "bytes": dest.stat().st_size}


def save_dataset(data_dir: Path, store: ObjectStore | None = None, *, stamp: str | None = None) -> dict[str, Any]:
    """Push the working dataset up, then move the pointer.

    The order is the safety property. The tarball goes to a key nobody is reading yet; only once it
    is uploaded and its digest recorded does the pointer move to it. A run that dies between the two
    leaves an orphaned object and a pointer that still names the last complete dataset, which is the
    failure everyone would choose.
    """
    store = store or store_from_env()
    data_dir = Path(data_dir)
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    key = f"dataset/monitor-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "dataset.tar.gz"
        info = _tar([data_dir / p for p in DATASET_PARTS], data_dir, archive)
        data = archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        store.put(key, data, content_type="application/gzip")

        pointer = {"key": key, "sha256": digest, "bytes": len(data), "files": info["files"],
                   "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "parts": [p for p in DATASET_PARTS if (data_dir / p).exists()]}
        store.put(POINTER_KEY, json.dumps(pointer, indent=2).encode(),
                  content_type="application/json", overwrite=True)
    log.info("dataset saved: %s (%.1f MB, %d files)", key, len(data) / 1e6, info["files"])
    return pointer


def restore_dataset(data_dir: Path, store: ObjectStore | None = None) -> dict[str, Any]:
    """Pull the current dataset down into an empty working directory.

    Returns `{"restored": False}` when the store holds nothing yet, which is the first run and not
    an error - the caller decides whether to backfill or stop. A digest that does not match what the
    pointer recorded *is* an error: a truncated dataset that looks plausible is the one thing worse
    than no dataset.
    """
    store = store or store_from_env()
    data_dir = Path(data_dir)
    raw = store.get(POINTER_KEY)
    if not raw:
        log.info("no dataset in the store yet; this is a first run")
        return {"restored": False, "reason": "the store holds no dataset pointer yet"}
    pointer = json.loads(raw)

    blob = store.get(pointer["key"])
    if blob is None:
        raise StorageError(f"the pointer names {pointer['key']}, which is not in the store")
    digest = hashlib.sha256(blob).hexdigest()
    if digest != pointer["sha256"]:
        raise StorageError(
            f"the dataset does not match its recorded digest (expected {pointer['sha256'][:12]}, "
            f"got {digest[:12]}); it is not being unpacked")

    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "dataset.tar.gz"
        archive.write_bytes(blob)
        with tarfile.open(archive, "r:gz") as tf:
            # a tar member that escapes the destination is a classic archive attack, and this
            # archive comes from a store rather than from us
            for member in tf.getmembers():
                target = (data_dir / member.name).resolve()
                if not str(target).startswith(str(data_dir.resolve())):
                    raise StorageError(f"archive member escapes the data directory: {member.name!r}")
            tf.extractall(data_dir)
    log.info("dataset restored from %s (%.1f MB)", pointer["key"], len(blob) / 1e6)
    return {"restored": True, **pointer}


def prune_datasets(store: ObjectStore | None = None, keep: int = 7) -> dict[str, Any]:
    """Keep a few dataset snapshots as backups; the pointer's own object is never a candidate."""
    store = store or store_from_env()
    raw = store.get(POINTER_KEY)
    current = json.loads(raw)["key"] if raw else None
    blobs = sorted((b for b in store.list("dataset/") if b["key"].endswith(".tar.gz")),
                   key=lambda b: b["key"])
    doomed = [b for b in blobs[:-keep] if b["key"] != current] if len(blobs) > keep else []
    return {"snapshots": len(blobs), "current": current, "would_remove": [b["key"] for b in doomed],
            "note": "removal is left to the store's own retention; nothing is deleted here"}


# -------------------------------------------------------------------- the reports

def publish_edition(edition_dir: Path, report_type: str, edition: str, version: int,
                    store: ObjectStore | None = None) -> dict[str, Any]:
    """Upload one report version. Immutable: a key that exists is never rewritten.

    The manifest goes up last. Anything that lists the archive keys on the manifest, so a partly
    uploaded edition is not yet visible as an edition - the same discipline the local archive uses
    when it repoints `latest` only after a successful render.
    """
    store = store or store_from_env()
    edition_dir = Path(edition_dir)
    base = f"reports/{report_type}/{edition}/v{version}"
    uploaded, skipped = [], []
    manifest_bytes: bytes | None = None

    for path in sorted(edition_dir.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        key = f"{base}/{path.name}"
        if path.name == "manifest.json":
            manifest_bytes = path.read_bytes()
            continue
        if store.exists(key):
            skipped.append(key)
            continue
        store.put(key, path.read_bytes(), content_type=_content_type(path))
        uploaded.append(key)

    if manifest_bytes is not None and not store.exists(f"{base}/manifest.json"):
        store.put(f"{base}/manifest.json", manifest_bytes, content_type="application/json")
        uploaded.append(f"{base}/manifest.json")

    log.info("published %s %s v%s: %d file(s) uploaded, %d already present",
             report_type, edition, version, len(uploaded), len(skipped))
    return {"prefix": base, "uploaded": uploaded, "already_present": skipped}


def _content_type(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".json": "application/json",
        ".png": "image/png",
    }.get(path.suffix.lower(), "application/octet-stream")


# ----------------------------------------------------------- the engine's own hooks

def restore_command() -> str:
    """The command the engine runs before a cycle. Kept as a command, not a call, so the existing
    lock-restore-process-save structure is untouched."""
    return 'python -m azmonitor.cloud.publish restore'


def save_command() -> str:
    return 'python -m azmonitor.cloud.publish save'


def dataset_summary(data_dir: Path) -> dict[str, Any]:
    """What is on disk, for a log line that makes a restore auditable."""
    data_dir = Path(data_dir)
    out: dict[str, Any] = {}
    for part in DATASET_PARTS:
        p = data_dir / part
        if not p.exists():
            out[part] = None
        elif p.is_file():
            out[part] = {"bytes": p.stat().st_size}
        else:
            files = [f for f in p.rglob("*") if f.is_file()]
            out[part] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
    return out
