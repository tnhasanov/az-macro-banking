"""Looking inside a file before it leaves the building.

The distribution profile decides how the engine *renders*. It says nothing about what is already on
disk — and an archive of previously rendered reports is exactly what a first seed uploads.

That gap was not theoretical. Running `publish seed --with-reports` under the neutral profile, on
the archive this repository actually holds, uploaded **95 branded decks, 36 branded workbooks and
95 branded PDFs** to personal object storage. Every one carried the bank's logo or its palette. The
profile guard passed each time, correctly, because the profile was neutral; the files were not.

So the question asked of an artefact is no longer "which profile is active?" but "does this file
carry the organisation's identity?". A file cannot lie about its own contents.
"""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from azmonitor import config
from azmonitor.cloud import artifacts

from conftest import minimal_pdf


@pytest.fixture(autouse=True)
def _fresh_config():
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()
    yield
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()


def _deck(path: Path, *, image: bool = False, colour: str | None = None,
          text: str = "Loans to the economy") -> Path:
    """The smallest thing shaped like a PowerPoint, with whichever marker we want to test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        slide = ('<?xml version="1.0"?><p:sld xmlns:p="x" xmlns:a="y"><a:t>' + text + "</a:t>"
                 + (f'<a:srgbClr val="{colour}"/>' if colour else "")
                 + "</p:sld>")
        z.writestr("ppt/slides/slide1.xml", slide)
        if image:
            z.writestr("ppt/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"logo")
    return path


# ------------------------------------------------------------------ what it detects

def test_an_embedded_image_is_a_finding(tmp_path):
    """A neutral deck embeds no images at all, so any image is a logo until someone says otherwise."""
    findings = artifacts.inspect(_deck(tmp_path / "branded.pptx", image=True))
    assert any("image" in f for f in findings)


def test_a_brand_colour_is_a_finding_even_with_no_logo_and_no_name(tmp_path):
    """A deck can carry the identity in its palette with the name nowhere on it."""
    findings = artifacts.inspect(_deck(tmp_path / "purple.pptx", colour="6F00B6"))
    assert any("6F00B6" in f for f in findings)


def test_the_organisation_name_is_a_finding(tmp_path):
    findings = artifacts.inspect(
        _deck(tmp_path / "named.pptx", text="Azer-Turk Bank  |  For internal discussion"))
    assert any("names the organisation" in f for f in findings)


def test_the_name_is_matched_whatever_the_case(tmp_path):
    findings = artifacts.inspect(_deck(tmp_path / "shouty.pptx", text="AZER-TURK BANK"))
    assert any("names the organisation" in f for f in findings)


def test_a_neutral_deck_has_no_findings(tmp_path):
    assert artifacts.inspect(_deck(tmp_path / "clean.pptx")) == []


def test_a_file_that_cannot_be_read_is_a_finding_not_a_pass(tmp_path):
    """"We could not check" and "it is clean" are different answers."""
    broken = tmp_path / "truncated.pptx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    findings = artifacts.inspect(broken)
    assert findings and "could not be inspected" in findings[0]


# ---------------------------------------------------------------------- PDFs

# A converted deck is where the check was weakest. Of the 95 PDFs in this repository's archive, 78
# name the bank in their text and 17 do not — those 17 carry its logo on the cover and its brand
# purple on every slide, and a text-only check passed every one of them.


def test_a_pdf_that_names_the_organisation_is_a_finding(tmp_path):
    findings = artifacts.inspect(
        minimal_pdf(tmp_path / "named.pdf", text="Azer-Turk Bank  |  For internal discussion"))
    assert any("names the organisation" in f for f in findings)


def test_a_pdf_carrying_the_brand_palette_and_no_name_is_a_finding(tmp_path):
    """The 17-file case: converting a deck keeps the palette and can drop the footer."""
    findings = artifacts.inspect(
        minimal_pdf(tmp_path / "purple.pdf", text="Loans to the economy", colour="6F00B6"))
    assert any("6F00B6" in f for f in findings), findings


def test_a_pdf_is_not_a_finding_merely_for_carrying_images(tmp_path):
    """`no_embedded_images` catches a logo in a deck. In a PDF every chart is an image, so the
    same rule would refuse every file the engine has ever produced, branded or not."""
    assert artifacts.inspect(
        minimal_pdf(tmp_path / "charts.pdf", text="Loans to the economy", image=True)) == []


def test_a_neutral_pdf_has_no_findings(tmp_path):
    assert artifacts.inspect(minimal_pdf(tmp_path / "clean.pdf",
                                         text="Azerbaijan Macro & Banking Monitor",
                                         colour="1F3F7A", image=True)) == []


def test_a_file_that_is_not_a_readable_pdf_is_a_finding_not_a_pass(tmp_path):
    """The shape of the defect that reached CI.

    `pdftotext` reports a file it cannot parse by exiting non-zero with empty output, and empty
    output read as "no organisation named" — so a PDF the guard could not open was uploaded as
    clean. Reading it in-process turns the same case into an exception, which is already a finding.
    """
    broken = tmp_path / "truncated.pdf"
    broken.write_bytes(b"%PDF-1.7\nnot really a pdf\n")
    findings = artifacts.inspect(broken)
    assert findings and "could not be inspected" in findings[0]


def test_inspecting_a_pdf_shells_out_to_nothing(tmp_path, monkeypatch):
    """Why this is pinned: the guard used to call `pdftotext`, which nothing declared as a
    dependency. The test runner had poppler installed and CI did not, so the same file was clean
    on one machine and uninspectable on the other. An in-process reader cannot drift that way."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"the artefact guard shelled out to {args[0]!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    findings = artifacts.inspect(
        minimal_pdf(tmp_path / "named.pdf", text="Azer-Turk Bank", colour="6F00B6"))
    assert len(findings) == 2, findings


def test_report_metadata_is_inspected_too(tmp_path):
    """The manifest travels with the edition and carries the footer text."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"organisation_label": "Azer-Turk Bank"}), encoding="utf-8")
    assert any("names the organisation" in f for f in artifacts.inspect(manifest))


def test_the_markers_come_from_configuration_not_from_code():
    spec = artifacts.markers()
    assert spec["text"] and spec["colours"]
    assert spec["no_embedded_images"] is True


# ------------------------------------------------------- what it does to an upload

def _archive(root: Path, *, branded: bool) -> Path:
    out = root / "outputs" / "monthly" / "2026-07" / "v9_20260919T093250Z"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"generated_at": "2026-09-19T09:32:50+00:00"}))
    _deck(out / "report.pptx", image=branded, colour="6F00B6" if branded else None)
    # A real zip, because the guard fails closed on a file it cannot open — which is correct, and
    # which a `b"PK\x03\x04"` stub would trip on its way past the thing being tested.
    with zipfile.ZipFile(out / "report.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst><si><t>Loans</t></si></sst>')
    return root / "outputs"


def test_publishing_a_branded_archive_is_refused(tmp_path):
    from azmonitor.cloud import objectstore as OS
    from azmonitor.cloud.publish import RestrictedArtefact, _publish_local_editions

    store = OS.LocalObjectStore(tmp_path / "bucket")
    out_dir = _archive(tmp_path, branded=True)

    with pytest.raises(RestrictedArtefact) as excinfo:
        _publish_local_editions(out_dir, store, fence=None)

    # The message has to say why the neutral profile did not save them.
    assert "rendered under a branded profile" in str(excinfo.value)
    assert "AZMONITOR_PROFILE=neutral" in str(excinfo.value)


def test_nothing_is_uploaded_when_an_artefact_is_refused(tmp_path):
    """A partial upload — some editions in the store, the rest refused — would be worse."""
    from azmonitor.cloud import objectstore as OS
    from azmonitor.cloud.publish import RestrictedArtefact, _publish_local_editions

    store = OS.LocalObjectStore(tmp_path / "bucket")
    out_dir = _archive(tmp_path, branded=True)

    with pytest.raises(RestrictedArtefact):
        _publish_local_editions(out_dir, store, fence=None)

    assert store.list("reports/") == [], "no file may reach the store once one is refused"
    assert OS.read_catalog(store) == []


def test_publishing_a_neutral_archive_succeeds(tmp_path):
    from azmonitor.cloud import objectstore as OS
    from azmonitor.cloud.publish import _publish_local_editions

    store = OS.LocalObjectStore(tmp_path / "bucket")
    out_dir = _archive(tmp_path, branded=False)

    result = _publish_local_editions(out_dir, store, fence=None)
    assert result["editions_uploaded"] == 1
    assert len(OS.read_catalog(store)) == 1


def test_the_override_takes_the_same_exact_phrase(tmp_path, monkeypatch):
    """An operator who owns the content can override; a stray `1` cannot."""
    from azmonitor.cloud import objectstore as OS
    from azmonitor.cloud.publish import (UPLOAD_OVERRIDE, UPLOAD_OVERRIDE_VALUE,
                                          RestrictedArtefact, _publish_local_editions)

    out_dir = _archive(tmp_path, branded=True)
    for value in ("1", "true", "yes"):
        monkeypatch.setenv(UPLOAD_OVERRIDE, value)
        store = OS.LocalObjectStore(tmp_path / f"bucket-{value}")
        with pytest.raises(RestrictedArtefact):
            _publish_local_editions(out_dir, store, fence=None,
                                    allow_restricted=os.environ.get(UPLOAD_OVERRIDE)
                                    == UPLOAD_OVERRIDE_VALUE)

    monkeypatch.setenv(UPLOAD_OVERRIDE, UPLOAD_OVERRIDE_VALUE)
    store = OS.LocalObjectStore(tmp_path / "bucket-allowed")
    result = _publish_local_editions(out_dir, store, fence=None, allow_restricted=True)
    assert result["editions_uploaded"] == 1
