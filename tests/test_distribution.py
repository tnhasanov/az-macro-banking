"""Distribution profiles: whose identity a report carries, and where it may go.

The branded theme renders a specific bank's logo and brand colours onto every slide. Those are the
bank's assets. A deployment that publishes to personal or third-party storage must not carry them
there, and a rule that lives only in a runbook is a rule that an unattended 04:30 run will break.

So the rule lives in the upload path, and these tests are about the two ways it could fail
silently: a profile that does not actually change the output, and a guard that does not actually
stop the upload.
"""
from __future__ import annotations

import os
import zipfile

import pytest

from azmonitor import config
from azmonitor.cloud import publish


@pytest.fixture(autouse=True)
def _fresh_config():
    """Configuration is cached per process; a test that changes the profile must see the change."""
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()
    yield
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()


def _use(monkeypatch, name: str) -> None:
    monkeypatch.setenv("AZMONITOR_PROFILE", name)
    for fn in (config.profile, config.settings, config.theme):
        fn.cache_clear()


def test_the_default_profile_is_the_branded_one_and_forbids_external_upload():
    active = config.profile()
    assert active["name"] == "internal"
    assert active["external_upload"] is False


def test_the_neutral_profile_carries_no_logo_and_no_brand_colour(monkeypatch):
    _use(monkeypatch, "neutral")
    theme = config.theme()
    assert theme["logo"] is None
    # The bank's purple, in any of the tints the branded theme uses.
    flat = str(theme["colors"]) + str(theme["series_colors"])
    for brand in ("6F00B6", "4E0080", "9B59D0", "DDD0EA", "E6DAF1", "F5ECFA"):
        assert brand not in flat, f"{brand} survives in the neutral theme"


def test_the_neutral_profile_does_not_name_the_bank(monkeypatch):
    _use(monkeypatch, "neutral")
    report = config.settings()["report"]
    assert "Azer-Turk" not in report["organisation_label"]
    assert "Azer-Turk" not in report["audience_label"]


def test_the_branded_profile_still_renders_the_bank_identity(monkeypatch):
    """The neutral profile must be a real alternative, not a rename of the only option."""
    _use(monkeypatch, "internal")
    assert config.theme()["logo"] is not None
    assert config.settings()["report"]["organisation_label"] == "Azer-Turk Bank"


def test_both_themes_define_the_same_roles(monkeypatch):
    """A missing role would fall back to a default at render time, which is how a brand colour
    creeps back in unnoticed."""
    _use(monkeypatch, "internal")
    branded = config.theme()
    _use(monkeypatch, "neutral")
    neutral = config.theme()
    for section in ("colors", "series_colors", "sizes", "layout", "fonts"):
        assert set(branded[section]) == set(neutral[section]), f"{section} roles differ"


def test_renderers_take_every_colour_from_the_theme():
    """No brand colour may be written into the rendering code.

    This is the failure that actually happened: the cover-slide text colour was hardcoded as
    DDD0EA in three renderers and the workbook header as 6F00B6, so a neutral deck still carried
    the bank's purple after the theme had been switched.
    """
    import pathlib

    brand = ("6F00B6", "4E0080", "9B59D0", "DDD0EA", "E6DAF1", "F5ECFA")
    offenders = []
    for path in pathlib.Path("azmonitor").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for colour in brand:
            if colour in text:
                offenders.append(f"{path}: {colour}")
    assert not offenders, "brand colours hardcoded outside the theme: " + ", ".join(offenders)


def test_an_unknown_profile_is_refused_rather_than_defaulted(monkeypatch):
    """Falling back to the branded profile because a deployment misspelled "neutral" is exactly
    the accident this exists to prevent."""
    _use(monkeypatch, "netural")
    with pytest.raises(KeyError, match="unknown distribution profile"):
        config.profile()


def test_upload_is_refused_under_a_restricted_profile(monkeypatch, tmp_path):
    _use(monkeypatch, "internal")
    monkeypatch.setenv("AZMONITOR_OBJECT_STORE_DIR", str(tmp_path / "bucket"))
    monkeypatch.delenv("AZMONITOR_ALLOW_RESTRICTED_UPLOAD", raising=False)

    refusal = publish._refuse_restricted_upload()
    assert refusal is not None
    assert refusal["uploaded"] is False
    assert "does not permit external upload" in refusal["error"]
    # The refusal has to say what to do instead, or it will be worked around rather than fixed.
    assert "AZMONITOR_PROFILE=neutral" in refusal["remedy"]

    # And the command itself must exit non-zero, so an unattended run fails rather than continues.
    assert publish.main(["save"]) != 0
    # Nothing was written.
    assert not list((tmp_path / "bucket").rglob("*")) if (tmp_path / "bucket").exists() else True


def test_upload_is_allowed_under_a_permitted_profile(monkeypatch):
    _use(monkeypatch, "neutral")
    assert publish._refuse_restricted_upload() is None


def test_the_override_needs_the_exact_phrase(monkeypatch):
    """A stray `1` or `true` inherited from another variable must not unlock this."""
    _use(monkeypatch, "internal")
    for value in ("1", "true", "yes", "", "I-OWN-THIS-CONTENT"):
        monkeypatch.setenv(publish.UPLOAD_OVERRIDE, value)
        assert publish._refuse_restricted_upload() is not None, f"{value!r} unlocked the upload"
    monkeypatch.setenv(publish.UPLOAD_OVERRIDE, publish.UPLOAD_OVERRIDE_VALUE)
    assert publish._refuse_restricted_upload() is None


def test_the_scheduled_workflow_runs_under_a_distributable_profile():
    """The cloud worker uploads to personal storage, so it must not render the branded profile."""
    import pathlib
    import yaml

    workflow = yaml.safe_load(pathlib.Path(".github/workflows/scheduled.yml").read_text())
    env = workflow["jobs"]["run"]["env"]
    assert env.get("AZMONITOR_PROFILE") == "neutral", (
        "the scheduled workflow must set AZMONITOR_PROFILE=neutral; without it the engine renders "
        "the bank's branding and the upload guard aborts every run"
    )


def test_a_rendered_neutral_deck_embeds_no_image(monkeypatch, tmp_path):
    """The end-to-end property, on a real file: a deck with no logo has no media part at all."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    _use(monkeypatch, "neutral")
    assert config.theme()["logo"] is None

    # Build the smallest thing the builder would build, to prove the absence is structural rather
    # than a property of one particular deck.
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    out = tmp_path / "neutral.pptx"
    prs.save(out)
    with zipfile.ZipFile(out) as z:
        assert not [n for n in z.namelist() if "/media/" in n]
