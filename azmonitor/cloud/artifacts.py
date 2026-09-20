"""Looking inside a file before it leaves the building.

The distribution profile decides how the engine *renders*. It says nothing about what is already on
disk, and that gap is not theoretical: switching to the neutral profile makes new output clean while
every deck rendered earlier under the branded profile sits in `outputs/` unchanged. Seeding that
archive uploaded 95 branded decks, 36 branded workbooks and 78 branded PDFs to personal storage,
under a profile whose whole purpose is to prevent exactly that, without one complaint.

So the guard moved down a level. Before an artefact is uploaded it is opened and read, and the
question asked of it is not "which profile is active?" but "does this file carry the bank's
identity?". A file cannot lie about its own contents.

Three markers, declared in `config/distribution.yaml` rather than written here, so the check and the
profile it enforces are described in one place:

* **Text.** The organisation's name, in extracted text. The name is the strongest signal and the
  hardest to remove by accident.
* **Colours.** The brand palette, as Office XML spells it. A deck can carry the identity in its
  colours with the name nowhere on it.
* **Embedded images.** A neutral deck embeds none at all, so any image is a logo until someone says
  otherwise.
"""
from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from .. import config
from ..util.log import get_logger

log = get_logger("cloud.artifacts")

OFFICE_SUFFIXES = {".pptx", ".xlsx", ".docx"}
TEXT_SUFFIXES = {".json", ".txt", ".md", ".csv"}


def markers() -> dict[str, Any]:
    cfg = config.load_yaml(config.CONFIG_DIR / "distribution.yaml")
    return cfg.get("restricted_markers") or {}


def inspect(path: Path) -> list[str]:
    """What in this file identifies the organisation. Empty means nothing did.

    Never raises for an unreadable file: a file that cannot be opened is reported as a finding
    rather than waved through, because "we could not check" and "it is clean" are different answers.
    """
    path = Path(path)
    spec = markers()
    names = [str(t) for t in (spec.get("text") or [])]
    colours = [str(c).upper() for c in (spec.get("colours") or [])]
    no_images = bool(spec.get("no_embedded_images"))
    found: list[str] = []

    try:
        suffix = path.suffix.lower()
        if suffix in OFFICE_SUFFIXES:
            found += _inspect_office(path, names, colours, no_images)
        elif suffix == ".pdf":
            found += _inspect_pdf(path, names)
        elif suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            found += [f"names the organisation ({n})" for n in names if _mentions(text, n)]
    except Exception as exc:  # noqa: BLE001 - an unreadable artefact is a finding, not a pass
        return [f"could not be inspected ({type(exc).__name__}: {exc})"]
    return found


def _mentions(text: str, name: str) -> bool:
    return re.search(re.escape(name), text, re.IGNORECASE) is not None


def _inspect_office(path: Path, names: list[str], colours: list[str], no_images: bool) -> list[str]:
    found: list[str] = []
    with zipfile.ZipFile(path) as z:
        entries = z.namelist()
        images = [n for n in entries if "/media/" in n]
        if no_images and images:
            found.append(f"embeds {len(images)} image(s): {', '.join(images[:3])}")

        text_parts = []
        for name in entries:
            if not name.endswith(".xml"):
                continue
            body = z.read(name).decode("utf-8", "ignore")
            hits = {c for c in colours if c in body.upper()}
            if hits:
                found.append(f"uses brand colours {', '.join(sorted(hits))} in {name}")
                colours = [c for c in colours if c not in hits]   # report each colour once
            if "/slides/slide" in name or name.endswith("sharedStrings.xml"):
                text_parts.append(body)
    joined = " ".join(text_parts)
    found += [f"names the organisation ({n})" for n in names if _mentions(joined, n)]
    return found


def _inspect_pdf(path: Path, names: list[str]) -> list[str]:
    """PDF text is compressed, so it has to be extracted rather than searched for in the bytes."""
    try:
        text = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True,
                              timeout=120).stdout
    except FileNotFoundError:
        return ["could not be inspected (pdftotext is not installed)"]
    except subprocess.TimeoutExpired:
        return ["could not be inspected (pdftotext timed out)"]
    return [f"names the organisation ({n})" for n in names if _mentions(text, n)]


def screen(paths: list[Path]) -> dict[str, list[str]]:
    """Inspect several artefacts. Returns only the ones with findings."""
    out: dict[str, list[str]] = {}
    for path in paths:
        findings = inspect(path)
        if findings:
            out[str(path)] = findings
    return out
