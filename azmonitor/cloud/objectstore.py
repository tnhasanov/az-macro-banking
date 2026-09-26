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
import secrets
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from ..util.log import get_logger

log = get_logger("cloud.objectstore")

# What a run needs to resume. Split by how often it changes, because the two halves are wildly
# different sizes: the databases and state come to about 6 MB and change on every run, while the
# downloaded source documents are about 254 MB and change only when a source publishes. Shipping
# 254 MB back up after a run that changed nothing was most of the transfer bill for none of the
# value, so the static half is re-uploaded only when its contents actually differ.
MUTABLE_PARTS = ("monitor.sqlite", "deliveries.sqlite", "state")
STATIC_PARTS = ("raw",)
DATASET_PARTS = MUTABLE_PARTS + STATIC_PARTS
POINTER_KEY = "dataset/current.json"
# One immutable JSON object per published edition version. This, not the runner's disk, is what the
# dashboard's report catalogue is rebuilt from.
CATALOG_PREFIX = "catalog/"
CHUNK = 1024 * 1024


class Fence:
    """Something that can say whether this worker still owns the right to write.

    A protocol rather than an import, so storage does not depend on the lease and the lease does not
    depend on storage. `DatabaseLease` satisfies it.
    """

    def check(self, what: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


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

    def put_file(self, key: str, path: Path, *, content_type: str = "application/octet-stream",
                 overwrite: bool = False) -> dict[str, Any]:
        """Upload a file from disk. The dataset uses this rather than `put` so that 260 MB is
        never held in memory alongside an open SQLite database."""
        raise NotImplementedError

    def get_file(self, key: str, dest: Path) -> bool:
        """Download to a path. False means the object is not there, which is a fact, not an error."""
        raise NotImplementedError

    def stat(self, key: str) -> dict[str, Any] | None:
        """Metadata without the body, or None. One request, no transfer."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def check(self) -> dict[str, Any]:
        """Prove this store can be opened, writing nothing.

        Called before a run that will write, because the failure worth catching is not a typo — a
        bad credential fails on the first call either way — but the run that gets far enough to
        matter first: restore a dataset, spend ten minutes refreshing sources and rendering, and
        only then discover it cannot save.
        """
        raise NotImplementedError

    def url_for(self, key: str) -> str | None:
        """A URL the dashboard can proxy. None when the backend has no addressable URL.

        Always None for a private store: a private blob is only readable with the token, so there
        is no URL worth handing to anyone.
        """
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

    def put_file(self, key, path, *, content_type="application/octet-stream", overwrite=False):
        p = self._path(key)
        if p.exists() and not overwrite:
            raise StorageError(f"{key} already exists and this store does not overwrite by default")
        p.parent.mkdir(parents=True, exist_ok=True)
        # Copied through a temporary name and moved, so this backend fails the same way the real
        # one does: either the whole object appears or none of it.
        staged = p.with_suffix(p.suffix + ".partial")
        digest = hashlib.sha256()
        with open(path, "rb") as src, open(staged, "wb") as dst:
            for chunk in iter(lambda: src.read(CHUNK), b""):
                digest.update(chunk)
                dst.write(chunk)
        staged.replace(p)
        return {"key": key, "size": p.stat().st_size, "sha256": digest.hexdigest()}

    def get_file(self, key, dest):
        p = self._path(key)
        if not p.exists():
            return False
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".partial")
        with open(p, "rb") as src, open(partial, "wb") as dst:
            for chunk in iter(lambda: src.read(CHUNK), b""):
                dst.write(chunk)
        partial.replace(dest)
        return True

    def stat(self, key):
        p = self._path(key)
        if not p.exists():
            return None
        return {"key": key, "size": p.stat().st_size,
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))}

    def delete(self, key):
        p = self._path(key)
        if p.exists():
            p.unlink()

    def check(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return {"backend": "local", "credential": "none (a local directory)",
                "root": str(self.root), "reachable": True}

    def url_for(self, key):
        return None


#: Every environment variable that can authenticate to a Blob store, in the order the SDK resolves
#: them. Named here so the selector, the store and the tests all mean the same set.
CREDENTIAL_VARS = ("BLOB_READ_WRITE_TOKEN", "VERCEL_OIDC_TOKEN", "BLOB_STORE_ID")


def blob_credentials(token: str | None = None) -> tuple[str, dict[str, str]]:
    """Which credential opens the store, and the environment that carries it to the helper.

    A private store takes either of two, and which one is available is decided by where the code
    runs rather than by preference:

    * **OIDC** — ``VERCEL_OIDC_TOKEN`` with ``BLOB_STORE_ID``. Short-lived and rotated by Vercel,
      and the better credential. It is issued to Vercel's own runtimes, and to the CLI on a linked
      project; there is no way to obtain one on a GitHub Actions runner, so the worker cannot use
      it. The dashboard, which runs on Vercel, can and does.
    * **Read-write token** — ``BLOB_READ_WRITE_TOKEN``. Long-lived and static, and what Vercel
      documents for code running outside Vercel, a CI job included. It is scoped to a single store,
      which makes it the *narrower* credential here even though it is the static one: the
      alternative for CI is a Vercel account token, which reaches the whole account.

    Resolved in the SDK's own order so this never disagrees with the helper it calls. The value is
    never logged, and only the name of the kind is returned alongside it.
    """
    read_write = (token or os.environ.get("BLOB_READ_WRITE_TOKEN") or "").strip()
    if read_write:
        return "read-write token", {"BLOB_READ_WRITE_TOKEN": read_write}

    oidc = (os.environ.get("VERCEL_OIDC_TOKEN") or "").strip()
    store_id = (os.environ.get("BLOB_STORE_ID") or "").strip()
    if oidc and store_id:
        return "oidc", {"VERCEL_OIDC_TOKEN": oidc, "BLOB_STORE_ID": store_id}
    if oidc:
        raise StorageError(
            "VERCEL_OIDC_TOKEN is set but BLOB_STORE_ID is not; OIDC needs the store it names")
    if store_id:
        raise StorageError(
            "BLOB_STORE_ID is set but VERCEL_OIDC_TOKEN is not. Outside Vercel there is no OIDC "
            "token to pair it with; set BLOB_READ_WRITE_TOKEN instead")
    raise StorageError(
        "no Blob credentials: set BLOB_READ_WRITE_TOKEN (outside Vercel, including CI), or "
        "VERCEL_OIDC_TOKEN with BLOB_STORE_ID (on Vercel)")


class VercelBlobStore(ObjectStore):
    """Vercel Blob, private, through the SDK Vercel maintains.

    Every object is written with `access: 'private'`, which means it has no publicly readable URL at
    all: a read carries the read-write token in an Authorization header. The dashboard therefore has
    to stream report files through its own authenticated route, and a URL copied out of the network
    tab is worth nothing to anyone who is not signed in.

    The transport is `tools/blob/blob.mjs`, a thin wrapper over `@vercel/blob`. Shelling out to Node
    from Python looks like a detour, and the alternative was considered and rejected: the dataset is
    about 260 MB, and uploading that as a single PUT is not how the API expects to receive it. The
    SDK splits a large body into a multipart upload, retries the parts that fail, and distinguishes
    "not found" from "refused" from "the service is unavailable". Re-deriving all of that from
    observed behaviour is the guesswork this is here to avoid.

    Bytes move through files, not pipes. The helper writes a download to a temporary name and moves
    it into place only once it is complete, so an interrupted transfer leaves nothing that could be
    mistaken for a whole object.
    """

    HELPER = Path(__file__).resolve().parents[2] / "tools" / "blob" / "blob.mjs"
    NOT_FOUND = 3
    REFUSED = 4

    def __init__(self, token: str | None = None, prefix: str = "", *, helper: Path | None = None):
        self.credential, self._credential_env = blob_credentials(token)
        self.prefix = prefix.strip("/")
        self.helper = Path(helper) if helper else self.HELPER
        if not self.helper.exists():
            raise StorageError(
                f"the blob helper is missing at {self.helper}; run `npm ci` in tools/blob")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}".lstrip("/") if self.prefix else key

    def _run(self, *argv: str, timeout: int = 900) -> tuple[int, dict[str, Any]]:
        """Call the helper. Returns its exit code and the JSON it printed, if any."""
        # Only the credential this store resolved to is handed down, so a stale variable left in the
        # environment cannot quietly authenticate a different way than the one reported.
        env = {**os.environ, **self._credential_env}
        for name in CREDENTIAL_VARS:
            if name not in self._credential_env:
                env.pop(name, None)
        try:
            proc = subprocess.run([self._node(), str(self.helper), *argv], capture_output=True,
                                  text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise StorageError(f"blob {argv[0]} timed out after {timeout}s") from exc
        except OSError as exc:
            raise StorageError(f"could not run the blob helper: {exc}") from exc

        if proc.returncode in (self.NOT_FOUND, self.REFUSED):
            return proc.returncode, {}
        if proc.returncode != 0:
            # stderr carries the SDK's own message, and the helper's advice where it has any; it
            # names the object but never the credential. The cap is generous because the message
            # worth reading is the long one — an environment mismatch explains itself in a
            # paragraph, and truncating it leaves the symptom without the fix.
            raise StorageError(
                f"blob {' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
        try:
            return 0, json.loads(proc.stdout or "{}")
        except ValueError as exc:
            raise StorageError(f"blob {argv[0]} returned output that is not JSON") from exc

    @staticmethod
    def _node() -> str:
        return os.environ.get("AZMONITOR_NODE", "node")

    # ------------------------------------------------------------------ operations
    def put(self, key, data, *, content_type="application/octet-stream", overwrite=False):
        full = self._key(key)
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "payload"
            staged.write_bytes(data)
            return self.put_file(key, staged, content_type=content_type, overwrite=overwrite)

    def put_file(self, key: str, path: Path, *, content_type: str = "application/octet-stream",
                 overwrite: bool = False) -> dict[str, Any]:
        """Upload a file that is already on disk, without reading it into memory first.

        This is the path the dataset takes. `put` exists for small objects and is written in terms
        of this one, rather than the other way round, because a 260 MB bytes object in a worker
        that also holds the SQLite database open is worth not creating.
        """
        full = self._key(key)
        argv = ["put", "--key", full, "--file", str(path), "--content-type", content_type]
        if overwrite:
            argv.append("--overwrite")
        code, meta = self._run(*argv)
        if code == self.REFUSED:
            raise StorageError(f"{full} already exists; published editions are immutable")
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
        return {"key": full, "size": meta.get("size", path.stat().st_size),
                "sha256": digest.hexdigest()}

    def get(self, key):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "payload"
            if not self.get_file(key, dest):
                return None
            return dest.read_bytes()

    def get_file(self, key: str, dest: Path) -> bool:
        """Download to a path. False when the object is not there; that is a fact, not an error."""
        code, _ = self._run("get", "--key", self._key(key), "--out", str(dest))
        return code == 0

    def exists(self, key):
        code, _ = self._run("head", "--key", self._key(key), timeout=120)
        return code == 0

    def stat(self, key: str) -> dict[str, Any] | None:
        code, meta = self._run("head", "--key", self._key(key), timeout=120)
        return meta if code == 0 else None

    def list(self, prefix):
        _, payload = self._run("list", "--prefix", self._key(prefix), timeout=300)
        return payload.get("blobs", [])

    def delete(self, key: str) -> None:
        self._run("del", "--key", self._key(key), timeout=120)

    def check(self):
        """One listing, so the credential is proved against the real store before anything writes.

        A listing is the cheapest call that still requires the credential to be valid for *this*
        store: a wrong or revoked one is refused by the service, and an empty store answers with an
        empty page rather than an error, so a first run is not mistaken for a failure.
        """
        _, payload = self._run("check", timeout=120)
        return {"backend": "vercel-blob", "credential": self.credential,
                "store_id": payload.get("store_id"), "prefix": self.prefix or None,
                "reachable": bool(payload.get("reachable")),
                "store_is_empty": not payload.get("objects_seen")
                and not payload.get("store_has_more")}

    def url_for(self, key):
        # A private blob has no URL anyone can use without a credential, so there is nothing to
        # hand out. Returning None is what keeps a caller from trying.
        return None


def store_from_env() -> ObjectStore:
    """The store this deployment uses, chosen by what is configured.

    Order matters: an explicitly configured local directory wins, so a test or a local run never
    reaches for the network by accident just because a token happens to be in the environment.
    """
    local = os.environ.get("AZMONITOR_OBJECT_STORE_DIR")
    if local:
        return LocalObjectStore(local)
    if any(os.environ.get(name) for name in CREDENTIAL_VARS):
        return VercelBlobStore(prefix=os.environ.get("AZMONITOR_BLOB_PREFIX", ""))
    raise StorageError(
        "no object store is configured: set AZMONITOR_OBJECT_STORE_DIR for a local directory, or "
        "a Vercel Blob credential — BLOB_READ_WRITE_TOKEN outside Vercel, or VERCEL_OIDC_TOKEN "
        "with BLOB_STORE_ID on it")


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


def _tree_fingerprint(paths: Iterable[Path], base: Path) -> str:
    """A digest of what a set of files *is*, without reading their contents.

    Path, size and modification time. `tarfile` preserves mtime through a round trip, so a restored
    tree fingerprints the same as the one that was saved, which is what makes "has the raw archive
    changed since the last run?" answerable on a fresh worker that has only just unpacked it.

    Hashing 254 MB of contents would also work and would be stricter; it would also cost a second
    of CPU on every run to answer a question that path-size-mtime answers correctly for an archive
    that only ever gains whole new files.
    """
    digest = hashlib.sha256()
    for root in sorted(paths):
        if not root.exists():
            continue
        entries = [root] if root.is_file() else sorted(root.rglob("*"))
        for entry in entries:
            if not entry.is_file():
                continue
            stat = entry.stat()
            digest.update(f"{entry.relative_to(base)}|{stat.st_size}|{int(stat.st_mtime)}\n".encode())
    return digest.hexdigest()


def _checkpoint_databases(data_dir: Path) -> None:
    """Fold every SQLite write-ahead log into its database before the database is copied.

    The engine opens its databases in WAL mode, where a committed transaction lives in
    `<name>-wal` until a checkpoint copies it into the main file. The dataset archive carries the
    main file only. Saving while a connection was still open — the job worker saves straight after
    collecting, before it produces anything — therefore archived a database without its newest
    writes, and a check that went on to produce nothing never saved again: the documents it had
    just read were lost from the stored dataset. Found by the end-to-end run.

    A TRUNCATE checkpoint from a fresh connection copies the log in and empties it, provided no
    other connection is mid-transaction. If the log is not empty afterwards the save is refused,
    because an archive without those writes is not the dataset.
    """
    import sqlite3

    for part in MUTABLE_PARTS:
        db = Path(data_dir) / part
        wal = db.with_name(db.name + "-wal")
        if db.suffix != ".sqlite" or not db.exists() or not wal.exists():
            continue                                   # no log, nothing to fold in
        con = sqlite3.connect(db, timeout=60)
        try:
            busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        if busy or (wal.exists() and wal.stat().st_size > 0):
            raise StorageError(f"{part} has writes that could not be checkpointed into it; the dataset was not "
                               f"saved rather than saved without them")


def _clear_stale_logs(data_dir: Path) -> None:
    """A write-ahead log left by an earlier process must not be replayed onto a restored database."""
    for part in MUTABLE_PARTS:
        for suffix in ("-wal", "-shm"):
            stale = Path(data_dir) / f"{part}{suffix}"
            if stale.exists():
                stale.unlink()


def save_dataset(data_dir: Path, store: ObjectStore | None = None, *, stamp: str | None = None,
                 fence: "Fence | None" = None) -> dict[str, Any]:
    """Push the working dataset up, then move the pointer.

    The order is the safety property. Each tarball goes to a key nobody is reading yet; only once
    both are uploaded and their digests recorded does the pointer move. A run that dies between the
    two leaves orphaned objects and a pointer that still names the last complete dataset, which is
    the failure everyone would choose.

    The static half — the downloaded source documents — is only re-uploaded when its fingerprint
    differs from the one the current pointer records. A source check that finds nothing therefore
    ships about 6 MB rather than 260 MB, and the pointer simply keeps naming the raw object that is
    already there. That object is never deleted while a pointer refers to it.

    `fence` is checked twice, and the second check is the one that matters: immediately before the
    pointer moves. A worker whose lease lapsed mid-upload has been replaced, and the replacement may
    already have written a newer dataset — letting the slow worker move the pointer afterwards would
    silently roll the dataset back to its older copy.
    """
    store = store or store_from_env()
    data_dir = Path(data_dir)
    # Seconds alone are not unique: a source check saves once after collecting and again after
    # publishing, and a fast run does both inside one second. The second upload then collided with
    # the first (objects are never overwritten) and the save failed. The suffix keeps keys unique
    # while still sorting by time.
    stamp = stamp or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"

    if fence:
        fence.check("before uploading the dataset")
    _checkpoint_databases(data_dir)

    previous = _current_pointer(store)
    static_fingerprint = _tree_fingerprint([data_dir / p for p in STATIC_PARTS], data_dir)
    reused = (previous or {}).get("static") or {}
    reuse_static = bool(reused.get("fingerprint") == static_fingerprint and reused.get("key")
                        and store.exists(reused["key"]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mutable_key = f"dataset/state-{stamp}.tar.gz"
        mutable_archive = tmp / "mutable.tar.gz"
        mutable_info = _tar([data_dir / p for p in MUTABLE_PARTS], data_dir, mutable_archive)
        mutable_meta = store.put_file(mutable_key, mutable_archive, content_type="application/gzip")

        if reuse_static:
            static = dict(reused)
            log.info("raw documents unchanged; reusing %s (%.1f MB not re-uploaded)",
                     static["key"], static.get("bytes", 0) / 1e6)
        else:
            static_key = f"dataset/raw-{stamp}.tar.gz"
            static_archive = tmp / "static.tar.gz"
            static_info = _tar([data_dir / p for p in STATIC_PARTS], data_dir, static_archive)
            static_meta = store.put_file(static_key, static_archive, content_type="application/gzip")
            static = {"key": static_key, "sha256": static_meta["sha256"],
                      "bytes": static_archive.stat().st_size, "files": static_info["files"],
                      "fingerprint": static_fingerprint}

        pointer = {
            "version": 2,
            "mutable": {"key": mutable_key, "sha256": mutable_meta["sha256"],
                        "bytes": mutable_archive.stat().st_size, "files": mutable_info["files"]},
            "static": static,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parts": [p for p in DATASET_PARTS if (data_dir / p).exists()],
            "static_reused": reuse_static,
        }
        pointer["bytes"] = pointer["mutable"]["bytes"] + static.get("bytes", 0)
        pointer["files"] = pointer["mutable"]["files"] + static.get("files", 0)
        pointer["uploaded_bytes"] = pointer["mutable"]["bytes"] + (0 if reuse_static else static["bytes"])

        # The uploads can take minutes. Ownership is re-checked here, with the objects already
        # safely in the store, so losing the lease costs an orphaned tarball rather than the dataset.
        if fence:
            fence.check("before moving the dataset pointer")

        store.put(POINTER_KEY, json.dumps(pointer, indent=2).encode(),
                  content_type="application/json", overwrite=True)

    log.info("dataset saved: %.1f MB uploaded of %.1f MB total (%d files)",
             pointer["uploaded_bytes"] / 1e6, pointer["bytes"] / 1e6, pointer["files"])
    return pointer


def _current_pointer(store: ObjectStore) -> dict[str, Any] | None:
    raw = store.get(POINTER_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def restore_dataset(data_dir: Path, store: ObjectStore | None = None) -> dict[str, Any]:
    """Pull the current dataset down into an empty working directory.

    Returns `{"restored": False}` when the store holds nothing, and says which of the two very
    different situations that is. A truly empty store is a first run. A store that holds dataset
    objects but no pointer, or a pointer naming an object that has gone, is not a first run — it is
    damage, and treating it as a first run is how a backfill overwrites a good dataset with an empty
    one. The caller is told which, and `safe_to_backfill` is only ever true for the first case.

    A digest that does not match what the pointer recorded is always an error: a truncated dataset
    that looks plausible is the one thing worse than no dataset. Both halves are checked; a stale
    raw archive reused across runs is verified on every restore, not merely on the run that wrote it.
    """
    store = store or store_from_env()
    data_dir = Path(data_dir)
    raw = store.get(POINTER_KEY)

    if not raw:
        # Is the store empty, or has the pointer gone missing from a store that clearly held one?
        debris = [b for b in store.list("dataset/") if b["key"].endswith(".tar.gz")]
        reports = store.list(CATALOG_PREFIX)
        if debris or reports:
            raise StorageError(
                f"the dataset pointer {POINTER_KEY} is missing, but the store holds "
                f"{len(debris)} dataset snapshot(s) and {len(reports)} catalogue entries. This is "
                f"not a first run: either the pointer was deleted or the store is misconfigured. "
                f"Refusing to continue, because a backfill from here would replace a real dataset "
                f"with an empty one. Restore the pointer from the newest snapshot, or set "
                f"AZMONITOR_ALLOW_EMPTY_STORE=yes if this store really is meant to be empty.")
        log.info("no dataset in the store and nothing else in it either; this is a first run")
        return {"restored": False, "reason": "the store holds no dataset pointer yet",
                "safe_to_backfill": True}

    pointer = json.loads(raw)
    data_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_logs(data_dir)

    # A pointer written before the dataset was split names one object; one written since names two.
    # Both are restored the same way, so a store seeded earlier keeps working.
    pieces = ([pointer["mutable"], pointer["static"]] if pointer.get("version", 1) >= 2
              else [{"key": pointer["key"], "sha256": pointer["sha256"]}])

    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for piece in pieces:
            archive = Path(tmp) / f"{Path(piece['key']).name}"
            if not store.get_file(piece["key"], archive):
                raise StorageError(
                    f"the pointer names {piece['key']}, which is not in the store. The dataset is "
                    f"not being replaced; investigate before running again.")

            digest = hashlib.sha256()
            with open(archive, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != piece["sha256"]:
                raise StorageError(
                    f"{piece['key']} does not match its recorded digest (expected "
                    f"{piece['sha256'][:12]}, got {digest.hexdigest()[:12]}); it is not being "
                    f"unpacked")
            total += archive.stat().st_size

            with tarfile.open(archive, "r:gz") as tf:
                # a tar member that escapes the destination is a classic archive attack, and this
                # archive comes from a store rather than from us
                for member in tf.getmembers():
                    target = (data_dir / member.name).resolve()
                    if not str(target).startswith(str(data_dir.resolve())):
                        raise StorageError(
                            f"archive member escapes the data directory: {member.name!r}")
                tf.extractall(data_dir)

    log.info("dataset restored: %d object(s), %.1f MB", len(pieces), total / 1e6)
    return {"restored": True, "safe_to_backfill": False, "objects": len(pieces), **pointer}


def prune_datasets(store: ObjectStore | None = None, keep: int = 7) -> dict[str, Any]:
    """Keep a few dataset snapshots as backups.

    Anything the current pointer names is never a candidate, and that now includes the raw archive,
    which a pointer can go on naming for weeks because it is only re-uploaded when it changes. A
    retention rule that deleted it because it looked old would break every restore afterwards.
    """
    store = store or store_from_env()
    pointer = _current_pointer(store) or {}
    in_use = {piece.get("key") for piece in
              (pointer.get("mutable"), pointer.get("static")) if isinstance(piece, dict)}
    in_use.add(pointer.get("key"))          # a pointer written before the split
    in_use.discard(None)

    blobs = sorted((b for b in store.list("dataset/") if b["key"].endswith(".tar.gz")),
                   key=lambda b: b["key"])
    candidates = [b for b in blobs if b["key"] not in in_use]
    doomed = candidates[:-keep] if len(candidates) > keep else []
    return {"snapshots": len(blobs), "in_use": sorted(in_use),
            "would_remove": [b["key"] for b in doomed],
            "note": "removal is left to the store's own retention; nothing is deleted here"}


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

    if manifest_bytes is not None:
        manifest_key = f"{base}/manifest.json"
        if store.exists(manifest_key):
            # Recorded rather than passed over in silence, so the two lists always account for
            # every file in the edition and a re-run can be read as "nothing new" at a glance.
            skipped.append(manifest_key)
        else:
            store.put(manifest_key, manifest_bytes, content_type="application/json")
            uploaded.append(manifest_key)

    log.info("published %s %s v%s: %d file(s) uploaded, %d already present",
             report_type, edition, version, len(uploaded), len(skipped))
    return {"prefix": base, "uploaded": uploaded, "already_present": skipped}


def catalog_key(report_type: str, edition: str, version: int) -> str:
    return f"{CATALOG_PREFIX}{report_type}/{edition}/v{version}.json"


def write_catalog_entry(entry: dict[str, Any], store: ObjectStore | None = None) -> str:
    """Record one published edition version, durably and once.

    This exists because the dashboard's catalogue used to be rebuilt from whatever happened to be
    on the runner's disk. A runner starts empty, so the run after a publish would find no editions
    and truncate the catalogue — every report ever produced would vanish from the dashboard while
    its files sat untouched in the store. The catalogue now lives beside the files it describes.

    An entry is written once and never rewritten, for the same reason the edition is: the metadata
    describes what was produced at a moment, and rewriting it later would change the record of what
    was sent.
    """
    store = store or store_from_env()
    key = catalog_key(entry["report_type"], entry["edition"], int(entry["version"]))
    if store.exists(key):
        return key
    store.put(key, json.dumps(entry, indent=2, default=str).encode(),
              content_type="application/json")
    log.info("catalogued %s", key)
    return key


def read_catalog(store: ObjectStore | None = None, *, verify_files: bool = True) -> list[dict[str, Any]]:
    """Every edition the store knows about, newest first.

    `verify_files` checks that each file an entry claims is actually in the store, and marks the
    entry rather than dropping it. A missing file is worth showing as a broken download; silently
    removing the edition would make a storage problem look like a report that was never produced.
    """
    store = store or store_from_env()
    entries: list[dict[str, Any]] = []
    present: set[str] | None = None
    if verify_files:
        present = {b["key"] for b in store.list("reports/")}

    for blob in store.list(CATALOG_PREFIX):
        raw = store.get(blob["key"])
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            log.warning("catalogue entry %s is not valid JSON; skipping it", blob["key"])
            continue
        if present is not None:
            missing = [f["key"] for f in entry.get("files") or [] if f.get("key") not in present]
            entry["files_missing"] = missing
            if missing:
                log.warning("%s %s v%s lists %d file(s) the store does not hold",
                            entry.get("report_type"), entry.get("edition"), entry.get("version"),
                            len(missing))
        entries.append(entry)

    entries.sort(key=lambda e: (str(e.get("generated_at") or ""), int(e.get("version") or 0)),
                 reverse=True)
    _mark_latest(entries)
    return entries


def _mark_latest(entries: list[dict[str, Any]]) -> None:
    """Flag the newest version of each edition, and the newest edition of each report type.

    Computed over the whole catalogue rather than over one run's output, because "latest" is a
    property of the archive and a single run only ever sees part of it.
    """
    best_version: dict[tuple[str, str], int] = {}
    for e in entries:
        k = (e.get("report_type", ""), e.get("edition", ""))
        best_version[k] = max(best_version.get(k, -1), int(e.get("version") or 0))
    for e in entries:
        k = (e.get("report_type", ""), e.get("edition", ""))
        e["is_latest"] = int(e.get("version") or 0) == best_version[k]


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
