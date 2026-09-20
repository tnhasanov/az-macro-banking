#!/usr/bin/env python3
"""Fill translation_available_at for publications collected before the column existed.

The original release date is already stored; what was missing is when the translated edition
appeared. That is recoverable from the translated file itself: its Last-Modified header, or the
date the publication page showed for it. Where neither exists the field stays empty rather than
being guessed, because an unknown translation date is not the same as "same day as the original".

Run once after upgrading a persistent dataset:  python scripts/backfill_translation_dates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azmonitor import config                                    # noqa: E402
from azmonitor.storage.db import Database                       # noqa: E402


def main() -> int:
    db = Database(config.paths().db_path)
    originals = {}
    for _sid, _scfg, ds in config.iter_datasets():
        lang = (ds.get("parse") or {}).get("original_language")
        if lang:
            originals[ds["id"]] = lang

    filled = skipped = 0
    for pub in db.publications():
        pub_id = pub["publication_id"]
        docs = db.conn.execute(
            "SELECT d.language, d.http_last_modified, d.published_at, d.dataset_id "
            "FROM publication_documents pd JOIN documents d ON d.doc_id = pd.doc_id "
            "WHERE pd.publication_id = ?", (pub_id,)).fetchall()
        if not docs:
            continue
        original = originals.get(docs[0]["dataset_id"], "az")
        translated = [d for d in docs if d["language"] and d["language"] != original]
        stamp = next((d["published_at"] for d in translated if d["published_at"]), None)
        basis = "date shown on the publication page" if stamp else None
        if not stamp:
            from azmonitor.pipeline import _http_date
            for d in translated:
                if d["http_last_modified"]:
                    parsed = _http_date(d["http_last_modified"])
                    if parsed:
                        stamp = parsed.isoformat()
                        basis = "HTTP Last-Modified header of the translated file (upload date)"
                        break
        rec = {"publication_id": pub_id, "original_language": original}
        if stamp:
            rec.update({"translation_available_at": stamp, "translation_available_basis": basis})
            filled += 1
        else:
            skipped += 1
        db.upsert_publication(rec)

    print(f"{filled} publication(s) given a translation date, {skipped} left unknown")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
