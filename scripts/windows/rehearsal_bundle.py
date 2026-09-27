"""Build a stand-in for the bootstrap bundle, under the real bundle's exact file names.

For rehearsing `seed-from-bundle.ps1` — the Windows job in CI — without the real dataset, which is
never committed or published. The contents are synthetic: a small monitor.sqlite that SQLite vouches
for, and a few raw files of incompressible bytes so the source archive is big enough to split into
nine real parts. It is laid out by `save_dataset` under the real bundle's stamp, the source archive
is split into the same nine parts, and both checksum files are written the way `sha256sum` writes
them — so the script sees exactly the names and formats it will see on the owner's machine.

The raw archive stays under the Blob helper's 8 MB multipart threshold, which the stand-in store
(tools/blob/fake-store.mjs) cannot serve.

    python scripts/windows/rehearsal_bundle.py <folder standing in for Downloads>
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

STAMP = "20260920T093543Z-snapshot"
PARTS = 9


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synthetic_dataset(data: Path) -> None:
    from azmonitor.storage.db import Database

    data.mkdir(parents=True)
    (data / "raw").mkdir()
    db = Database(data / "monitor.sqlite")
    for i in range(6):
        body = os.urandom(600_000)
        name = f"rehearsal_{i}.xlsx"
        (data / "raw" / name).write_bytes(body)
        db.conn.execute(
            "INSERT INTO documents(doc_id, source_id, dataset_id, document_url, sha256, retrieved_at, "
            "first_seen_at) VALUES (?, 'rehearsal', 'rehearsal', ?, ?, '2026-09-20T09:35:01Z', "
            "'2026-09-20T09:35:01Z')",
            (f"d{i}", f"https://example.az/{name}", hashlib.sha256(body).hexdigest()))
    db.conn.commit()
    db.close()
    # The other two parts the real bundle carries, so every part goes through the same path.
    ledger = sqlite3.connect(data / "deliveries.sqlite")
    ledger.execute("CREATE TABLE rehearsal (id INTEGER PRIMARY KEY)")
    ledger.commit()
    ledger.close()
    (data / "state").mkdir()
    (data / "state" / "rehearsal.json").write_text('{"rehearsal": true}')


def main(out: Path) -> int:
    from azmonitor.cloud import objectstore as OS

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        synthetic_dataset(Path(tmp) / "data")
        bundle = Path(tmp) / "azmonitor-bootstrap"
        OS.save_dataset(Path(tmp) / "data", OS.LocalObjectStore(bundle), stamp=STAMP)
        dataset = bundle / "dataset"
        names = ["current.json", f"state-{STAMP}.tar.gz", f"raw-{STAMP}.tar.gz"]
        missing = [n for n in names if not (dataset / n).is_file()]
        if missing:
            print(f"save_dataset did not produce {missing}", file=sys.stderr)
            return 1
        (out / "SHA256SUMS").write_text(
            "".join(f"{sha256(dataset / n)}  dataset/{n}\n" for n in names), newline="\n")
        shutil.copyfile(dataset / "current.json", out / "current.json")
        shutil.copyfile(dataset / names[1], out / names[1])

        raw = (dataset / names[2]).read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            print("the stand-in source archive is above the multipart threshold", file=sys.stderr)
            return 1
        size = -(-len(raw) // PARTS)
        lines = []
        for i in range(PARTS):
            part = out / f"{names[2]}.part-{i:02d}"
            part.write_bytes(raw[i * size:(i + 1) * size])
            lines.append(f"{sha256(part)}  {part.name}\n")
        (out / "PARTS.SHA256SUMS").write_text("".join(lines), newline="\n")
    print(json.dumps({"downloads": str(out), "files": sorted(p.name for p in out.iterdir()),
                      "raw_bytes": len(raw), "part_bytes": size}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
