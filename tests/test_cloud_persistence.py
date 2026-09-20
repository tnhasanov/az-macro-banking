"""What survives when the machine does not.

Every test here is about the gap between "the run succeeded" and "the result is still there
afterwards". A GitHub Actions runner starts with an empty disk and is destroyed at the end, so
anything the next run needs has to be in object storage, and anything derived from the runner's
local filesystem is derived from nothing.

The case that motivated the file: the dashboard's report catalogue was rebuilt from
`archive.index()`, which reads `outputs/` on the local disk, and published with `TRUNCATE editions`.
Runner A produced a report and uploaded it. Runner B started empty, found no local editions, and
truncated the catalogue — every report ever produced disappeared from the dashboard while its files
sat untouched in the store. `test_runner_b_does_not_erase_runner_as_history` is that scenario.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from azmonitor.cloud import objectstore as OS


# --------------------------------------------------------------- building fixtures

def _edition_on_disk(root: Path, report_type: str, edition: str, version: int,
                     *, fingerprint: str = "abc123") -> Path:
    """A rendered edition, as the renderer leaves it on the runner's disk."""
    d = root / report_type / edition / f"v{version}_20260919T093250Z"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "report_type": report_type, "edition": edition, "version": version,
        "generated_at": "2026-09-19T09:32:50+00:00", "as_of": "2026-07-31",
        "status_label": "Draft for review", "n_slides": 27,
        "edition_fingerprint": {"fingerprint": fingerprint},
        "edition_trigger": {"trigger": "new_publication"},
        "narrative_mode": "analyst", "narrative_validation": {"numbers_checked": 118},
        "quality_summary": {"checks": 40, "failed": 5, "critical": 0},
        "reporting_periods": {"banking": "2026-07-31", "macro": "2026-08-31"},
    }), encoding="utf-8")
    (d / "narrative.json").write_text(json.dumps({"mode": "analyst", "slides": {}}), encoding="utf-8")
    (d / f"report_{edition}_v{version}.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 2048)
    (d / f"report_{edition}_v{version}.pptx").write_bytes(b"PK\x03\x04" + b"y" * 1024)
    (d / f"report_{edition}_v{version}.xlsx").write_bytes(b"PK\x03\x04" + b"z" * 512)
    return d


def _publish(store, version_dir: Path, report_type: str, edition: str, version: int) -> dict:
    """What `publish save` does for one edition: files first, then the catalogue entry."""
    from azmonitor.cloud.publish import _catalog_entry

    OS.publish_edition(version_dir, report_type, edition, version, store)
    entry = _catalog_entry(version_dir, report_type, edition, version)
    OS.write_catalog_entry(entry, store)
    return entry


# ------------------------------------------------------------- the catalogue itself

def test_a_catalogue_entry_records_what_the_edition_claimed(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9)
    entry = _publish(store, d, "monthly", "2026-07", 9)

    # Everything a reader needs to judge the figure, kept with the figure.
    assert entry["fingerprint"] == "abc123"
    assert entry["generated_at"] == "2026-09-19T09:32:50+00:00"
    assert entry["reporting_periods"] == {"banking": "2026-07-31", "macro": "2026-08-31"}
    assert entry["quality"] == {"checks": 40, "failed": 5, "critical": 0}
    assert entry["numbers_checked"] == 118
    assert {f["name"].rsplit(".", 1)[-1] for f in entry["files"]} == {"pdf", "pptx", "xlsx"}


def test_a_catalogue_entry_is_written_once_and_never_rewritten(tmp_path):
    """An edition version is immutable, so the record of it is too."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9, fingerprint="first")
    _publish(store, d, "monthly", "2026-07", 9)

    # The same version rendered again with different metadata must not overwrite the record.
    d2 = _edition_on_disk(tmp_path / "outputs2", "monthly", "2026-07", 9, fingerprint="second")
    _publish(store, d2, "monthly", "2026-07", 9)

    entries = OS.read_catalog(store)
    assert len(entries) == 1
    assert entries[0]["fingerprint"] == "first"


def test_the_catalogue_marks_the_newest_version_of_each_edition(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    for v in (7, 8, 9):
        d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", v)
        _publish(store, d, "monthly", "2026-07", v)

    entries = OS.read_catalog(store)
    assert len(entries) == 3, "older versions must stay in the archive"
    latest = [e for e in entries if e["is_latest"]]
    assert [e["version"] for e in latest] == [9]


def test_a_catalogued_file_that_is_missing_is_reported_not_hidden(tmp_path):
    """A download that would 404 should show as broken, not as a report nobody produced."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9)
    _publish(store, d, "monthly", "2026-07", 9)

    store.delete("reports/monthly/2026-07/v9/report_2026-07_v9.pdf")

    entries = OS.read_catalog(store)
    assert len(entries) == 1, "the edition stays in the catalogue"
    assert entries[0]["files_missing"] == ["reports/monthly/2026-07/v9/report_2026-07_v9.pdf"]


# ----------------------------------------------------- two runners, one after another

def test_runner_b_does_not_erase_runner_as_history(tmp_path, monkeypatch):
    """The defect this file exists for, as a scenario.

    Runner A renders and publishes an edition, then its disk goes away. Runner B starts with
    nothing, restores the dataset, finds no source changes, produces no new report, and publishes
    the read model. Runner A's edition must still be listed and still be downloadable.
    """
    bucket = tmp_path / "bucket"
    store = OS.LocalObjectStore(bucket)

    # --- Runner A: render, publish, then vanish.
    a_outputs = tmp_path / "runner-a" / "outputs"
    a_data = tmp_path / "runner-a" / "data"
    (a_data / "state").mkdir(parents=True)
    (a_data / "monitor.sqlite").write_bytes(b"SQLite format 3\x00" + b"a" * 512)
    d = _edition_on_disk(a_outputs, "monthly", "2026-07", 9)
    _publish(store, d, "monthly", "2026-07", 9)
    OS.save_dataset(a_data, store)

    import shutil
    shutil.rmtree(tmp_path / "runner-a")          # the runner is destroyed

    # --- Runner B: empty disk, restores the dataset, nothing new to report.
    b_data = tmp_path / "runner-b" / "data"
    restored = OS.restore_dataset(b_data, store)
    assert restored["restored"] is True
    assert not (tmp_path / "runner-b" / "outputs").exists(), "a fresh runner has no reports on disk"

    # The catalogue is read from the store, so it does not depend on runner B's disk at all.
    catalogue = OS.read_catalog(store)
    assert len(catalogue) == 1
    assert catalogue[0]["report_type"] == "monthly"
    assert catalogue[0]["version"] == 9
    assert catalogue[0]["fingerprint"] == "abc123"
    assert catalogue[0]["files_missing"] == [], "runner A's files are still downloadable"

    # And every file the entry offers really is in the store.
    for f in catalogue[0]["files"]:
        assert store.exists(f["key"]), f"{f['key']} is listed but not stored"
        assert store.get(f["key"]), f"{f['key']} is empty"


def test_a_third_runner_adds_to_the_archive_rather_than_replacing_it(tmp_path):
    """Each run contributes; none of them is the archive."""
    store = OS.LocalObjectStore(tmp_path / "bucket")

    for run, (rt, ed, v) in enumerate([("monthly", "2026-07", 9), ("weekly", "2026-09-19", 14),
                                       ("monthly", "2026-08", 1)]):
        outputs = tmp_path / f"runner-{run}" / "outputs"
        d = _edition_on_disk(outputs, rt, ed, v)
        _publish(store, d, rt, ed, v)

    catalogue = OS.read_catalog(store)
    assert {(e["report_type"], e["edition"], e["version"]) for e in catalogue} == {
        ("monthly", "2026-07", 9), ("weekly", "2026-09-19", 14), ("monthly", "2026-08", 1)}


# ------------------------------------------------------------- interrupted transfers

def test_an_upload_that_dies_before_its_catalogue_entry_is_recoverable(tmp_path):
    """Files up, entry not written: the edition is invisible but nothing is lost.

    Re-running `save` writes the entry. The test is that the second attempt succeeds rather than
    tripping over the files that are already there.
    """
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9)

    OS.publish_edition(d, "monthly", "2026-07", 9, store)      # interrupted here
    assert OS.read_catalog(store) == []

    from azmonitor.cloud.publish import _catalog_entry
    OS.write_catalog_entry(_catalog_entry(d, "monthly", "2026-07", 9), store)  # the retry

    catalogue = OS.read_catalog(store)
    assert len(catalogue) == 1
    assert catalogue[0]["files_missing"] == []


def test_republishing_an_edition_whose_files_are_already_there_is_not_an_error(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9)

    first = OS.publish_edition(d, "monthly", "2026-07", 9, store)
    second = OS.publish_edition(d, "monthly", "2026-07", 9, store)
    assert first["uploaded"] and not second["uploaded"]
    assert len(second["already_present"]) == len(first["uploaded"])


def test_a_download_that_dies_leaves_no_half_file(tmp_path):
    """The local store moves a completed file into place, the same as the Blob helper does."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    store.put("reports/x.pdf", b"%PDF-1.7\n" + b"x" * 4096)

    dest = tmp_path / "out" / "x.pdf"
    assert store.get_file("reports/x.pdf", dest) is True
    assert dest.read_bytes().startswith(b"%PDF")
    assert not list(dest.parent.glob("*.partial")), "no temporary file is left behind"

    assert store.get_file("reports/missing.pdf", tmp_path / "out" / "missing.pdf") is False
    assert not (tmp_path / "out" / "missing.pdf").exists()


# ------------------------------------------------------- an empty store is not a licence

def test_a_store_with_reports_but_no_pointer_refuses_to_look_like_a_first_run(tmp_path):
    """The difference between "nothing here yet" and "the pointer is gone" is the dataset.

    Treating the second as the first is how a backfill replaces a real dataset with an empty one.
    """
    store = OS.LocalObjectStore(tmp_path / "bucket")
    d = _edition_on_disk(tmp_path / "outputs", "monthly", "2026-07", 9)
    _publish(store, d, "monthly", "2026-07", 9)

    with pytest.raises(OS.StorageError, match="not a first run"):
        OS.restore_dataset(tmp_path / "data", store)


def test_a_store_with_snapshots_but_no_pointer_also_refuses(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    (data / "monitor.sqlite").write_bytes(b"SQLite format 3\x00")
    OS.save_dataset(data, store)

    store.delete(OS.POINTER_KEY)

    with pytest.raises(OS.StorageError, match="not a first run"):
        OS.restore_dataset(tmp_path / "data2", store)


def test_a_genuinely_empty_store_is_a_first_run(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    result = OS.restore_dataset(tmp_path / "data", store)
    assert result["restored"] is False
    assert result["safe_to_backfill"] is True


# ------------------------------------------------------------------ seeding a store

def _seed(monkeypatch, tmp_path, store_dir: Path, data_dir: Path, out_dir: Path, *argv: str):
    """Run `publish seed` the way an operator would, and return its JSON and exit code."""
    import io
    import contextlib

    from azmonitor import config
    from azmonitor.cloud import publish

    monkeypatch.setenv("AZMONITOR_OBJECT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZMONITOR_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("AZMONITOR_PROFILE", "neutral")
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = publish.main(["seed", *argv])
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()
    return code, json.loads(out.getvalue())


def _dataset_on_disk(data_dir: Path) -> Path:
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    (data_dir / "monitor.sqlite").write_bytes(b"SQLite format 3\x00" + b"m" * 4096)
    (data_dir / "deliveries.sqlite").write_bytes(b"SQLite format 3\x00" + b"d" * 512)
    (data_dir / "raw" / "cba_loans.xlsx").write_bytes(b"PK\x03\x04" + b"r" * 1024)
    (data_dir / "state" / "last_source_check.json").write_text('{"status": "ok"}')
    return data_dir


def test_seeding_an_empty_store_uploads_and_verifies_the_dataset(monkeypatch, tmp_path):
    data = _dataset_on_disk(tmp_path / "data")
    code, result = _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, tmp_path / "outputs")
    assert code == 0
    assert result["seeded"] is True
    # Verified by reading back what the store now holds, not by trusting the upload call. Both
    # halves are checked: a dataset missing either one is not a smaller dataset, it is broken.
    assert {v["part"] for v in result["verified"]} == {"mutable", "static"}
    for piece in result["verified"]:
        assert piece["bytes_in_store"] == piece["bytes_expected"], piece["part"]
        assert len(piece["sha256"]) == 64


def test_seeding_a_store_that_already_holds_a_dataset_is_refused(monkeypatch, tmp_path):
    """The check that separates a first-time setup from overwriting a live dataset."""
    data = _dataset_on_disk(tmp_path / "data")
    _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, tmp_path / "outputs")

    code, result = _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, tmp_path / "outputs")
    assert code != 0
    assert result["seeded"] is False
    assert "already holds a dataset" in result["error"]
    assert "--replace" in result["remedy"]


def test_seeding_without_a_database_file_is_refused(monkeypatch, tmp_path):
    """An empty directory is not a dataset, and uploading it as one is how the real one is lost."""
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    code, result = _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, tmp_path / "outputs")
    assert code != 0
    assert "no monitor.sqlite" in result["error"]


def test_seeding_refuses_to_carry_files_that_are_not_the_dataset(monkeypatch, tmp_path):
    """Anything else in the directory would be swept into the tarball and sent to the cloud."""
    data = _dataset_on_disk(tmp_path / "data")
    (data / "board-pack-confidential.xlsx").write_bytes(b"PK\x03\x04")

    code, result = _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, tmp_path / "outputs")
    assert code != 0
    assert result["unexpected_files"] == ["board-pack-confidential.xlsx"]
    assert "would be uploaded with it" in result["error"]


def test_a_check_only_seed_uploads_nothing(monkeypatch, tmp_path):
    data = _dataset_on_disk(tmp_path / "data")
    bucket = tmp_path / "bucket"
    code, result = _seed(monkeypatch, tmp_path, bucket, data, tmp_path / "outputs", "--check")
    assert code == 0
    assert result["checked_only"] is True
    assert result["seeded"] is False
    assert not bucket.exists() or not list(bucket.rglob("*.tar.gz"))


def test_seeding_with_reports_gives_the_dashboard_history_from_the_first_run(monkeypatch, tmp_path):
    data = _dataset_on_disk(tmp_path / "data")
    outputs = tmp_path / "outputs"
    _edition_on_disk(outputs, "monthly", "2026-07", 9)
    _edition_on_disk(outputs, "weekly", "2026-09-19", 14)

    code, result = _seed(monkeypatch, tmp_path, tmp_path / "bucket", data, outputs, "--with-reports")
    assert code == 0
    assert result["reports"]["editions_uploaded"] == 2

    store = OS.LocalObjectStore(tmp_path / "bucket")
    catalogue = OS.read_catalog(store)
    assert {e["report_type"] for e in catalogue} == {"monthly", "weekly"}
    assert all(e["files_missing"] == [] for e in catalogue)


def test_two_directories_claiming_one_version_publish_once(tmp_path):
    """Repeated renders leave `v1_<stampA>` and `v1_<stampB>` on disk.

    Only one of them can be version 1 in the store, because a published version is immutable.
    Uploading both put two renders under one key and left the loser's files with no catalogue
    entry — orphans that `publish verify` then reported for ever.
    """
    from azmonitor.cloud.publish import _publish_local_editions

    store = OS.LocalObjectStore(tmp_path / "bucket")
    outputs = tmp_path / "outputs"
    first = outputs / "monthly" / "2026-07" / "v1_20260919T060252Z"
    second = outputs / "monthly" / "2026-07" / "v1_20260919T060512Z"
    for d, tag in ((first, "early"), (second, "late")):
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"generated_at": f"2026-09-19T0{len(tag)}:00:00"}))
        (d / f"report_{tag}.pdf").write_bytes(b"%PDF-1.7\n")

    _publish_local_editions(outputs, store, fence=None)

    catalogue = OS.read_catalog(store)
    assert len(catalogue) == 1, "one version number, one published edition"
    names = {f["name"] for f in catalogue[0]["files"]}
    assert names == {"report_late.pdf"}, "the newest render is the one that is published"

    # And nothing was uploaded that the catalogue does not account for.
    stored = {b["key"] for b in store.list("reports/") if b["key"].endswith(".pdf")}
    assert stored == {f["key"] for f in catalogue[0]["files"]}


# -------------------------------------------------- shipping only what actually changed

def test_an_unchanged_raw_archive_is_not_uploaded_twice(tmp_path):
    """The measured reason this exists: 5.9 MB changes every run, 254 MB does not.

    A source check that finds nothing used to ship the whole 260 MB back up for no reason. Now it
    ships the databases and state, and the pointer goes on naming the raw object already in the
    store.
    """
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")

    first = OS.save_dataset(data, store, stamp="20260920T000000Z")
    assert first["static_reused"] is False

    # A run that changed the database but downloaded nothing new.
    (data / "monitor.sqlite").write_bytes(b"SQLite format 3\x00" + b"changed" * 600)
    second = OS.save_dataset(data, store, stamp="20260920T010000Z")

    assert second["static_reused"] is True
    assert second["static"]["key"] == first["static"]["key"], "the pointer keeps the same raw object"
    assert second["mutable"]["key"] != first["mutable"]["key"], "the changed half is new"
    assert second["uploaded_bytes"] < second["bytes"], "less was sent than the dataset weighs"


def test_a_new_source_document_does_trigger_a_raw_upload(tmp_path):
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")
    first = OS.save_dataset(data, store, stamp="20260920T000000Z")

    (data / "raw" / "ssc_prices.pdf").write_bytes(b"%PDF-1.7\n" + b"new" * 400)
    second = OS.save_dataset(data, store, stamp="20260920T010000Z")

    assert second["static_reused"] is False
    assert second["static"]["key"] != first["static"]["key"]


def test_a_reused_archive_still_round_trips_and_is_still_verified(tmp_path):
    """A raw object can be named by a pointer for weeks. Every restore checks it, not just the
    run that wrote it."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")
    OS.save_dataset(data, store, stamp="20260920T000000Z")
    (data / "monitor.sqlite").write_bytes(b"SQLite format 3\x00" + b"later" * 700)
    pointer = OS.save_dataset(data, store, stamp="20260920T010000Z")
    assert pointer["static_reused"] is True

    fresh = tmp_path / "fresh"
    result = OS.restore_dataset(fresh, store)
    assert result["restored"] is True
    assert result["objects"] == 2
    assert (fresh / "raw" / "cba_loans.xlsx").read_bytes() == (data / "raw" / "cba_loans.xlsx").read_bytes()
    assert (fresh / "monitor.sqlite").read_bytes() == (data / "monitor.sqlite").read_bytes()

    # Corrupt the reused half and the next restore must refuse it.
    store.put(pointer["static"]["key"], b"corrupted", overwrite=True)
    with pytest.raises(OS.StorageError, match="does not match its recorded digest"):
        OS.restore_dataset(tmp_path / "fresh2", store)


def test_a_restored_dataset_fingerprints_the_same_as_the_one_that_was_saved(tmp_path):
    """The whole scheme rests on this: a fresh worker must reach the same answer about whether the
    raw archive changed, having only just unpacked it."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")
    saved = OS.save_dataset(data, store, stamp="20260920T000000Z")

    fresh = tmp_path / "fresh"
    OS.restore_dataset(fresh, store)
    after = OS._tree_fingerprint([fresh / p for p in OS.STATIC_PARTS], fresh)
    assert after == saved["static"]["fingerprint"], (
        "tarfile must preserve mtime through the round trip, or every run would re-upload 254 MB")

    # And a worker that restored then saved again reuses rather than re-uploading.
    again = OS.save_dataset(fresh, store, stamp="20260920T020000Z")
    assert again["static_reused"] is True


def test_retention_never_removes_an_object_a_pointer_still_names(tmp_path):
    """A reused raw archive looks old. Deleting it would break every restore afterwards."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")
    first = OS.save_dataset(data, store, stamp="20260920T000000Z")
    for hour in range(1, 10):
        (data / "monitor.sqlite").write_bytes(b"SQLite format 3\x00" + bytes([hour]) * 900)
        OS.save_dataset(data, store, stamp=f"202609{20:02d}T{hour:02d}0000Z")

    plan = OS.prune_datasets(store, keep=3)
    assert first["static"]["key"] in plan["in_use"]
    assert first["static"]["key"] not in plan["would_remove"]
    current = json.loads(store.get(OS.POINTER_KEY))
    assert current["mutable"]["key"] not in plan["would_remove"]


def test_a_pointer_written_before_the_split_still_restores(tmp_path):
    """A store seeded by an earlier version names one object. It must keep working."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    data = _dataset_on_disk(tmp_path / "data")

    # Build the old single-tarball shape by hand.
    import tarfile, tempfile, hashlib
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "d.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for part in OS.DATASET_PARTS:
                if (data / part).exists():
                    tf.add(data / part, arcname=part)
        blob = archive.read_bytes()
    store.put("dataset/monitor-legacy.tar.gz", blob, content_type="application/gzip")
    store.put(OS.POINTER_KEY, json.dumps({
        "key": "dataset/monitor-legacy.tar.gz",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob), "files": 5, "saved_at": "2026-09-01T00:00:00Z",
    }).encode(), content_type="application/json", overwrite=True)

    fresh = tmp_path / "fresh"
    result = OS.restore_dataset(fresh, store)
    assert result["restored"] is True
    assert result["objects"] == 1
    assert (fresh / "monitor.sqlite").exists() and (fresh / "raw" / "cba_loans.xlsx").exists()
