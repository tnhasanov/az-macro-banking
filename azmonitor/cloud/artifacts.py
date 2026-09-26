"""Looking inside a file before it leaves the building.

The distribution profile decides how the engine *renders*. It says nothing about what is already on
disk, and that gap is not theoretical: switching to the neutral profile makes new output clean while
every deck rendered earlier under the branded profile sits in `outputs/` unchanged. Seeding that
archive uploaded 95 branded decks, 36 branded workbooks and 95 branded PDFs to personal storage,
under a profile whose whole purpose is to prevent exactly that, without one complaint.

So the guard moved down a level. Before an artefact is uploaded it is opened and read, and the
question asked of it is not "which profile is active?" but "does this file carry the bank's
identity?". A file cannot lie about its own contents.

Three markers, declared in `config/distribution.yaml` rather than written here, so the check and the
profile it enforces are described in one place:

* **Text.** The organisation's name, in extracted text. The name is the strongest signal and the
  hardest to remove by accident.
* **Colours.** The brand palette, as Office XML spells it and as a PDF's content stream draws it.
  A file can carry the identity in its colours with the name nowhere on it — 17 of the 95 PDFs in
  this repository's archive do exactly that.
* **Embedded images.** A neutral deck embeds none at all, so any image in one is a logo until
  someone says otherwise. Not applied to PDFs, where every chart is an image.
"""
from __future__ import annotations

import re
import zipfile
from itertools import chain
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
            found += _inspect_pdf(path, names, colours)
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


def _inspect_pdf(path: Path, names: list[str], colours: list[str]) -> list[str]:
    """A PDF hides its text and its colours behind compression, so both have to be decoded.

    Read with pdfplumber, which the engine already depends on for parsing published PDFs, rather
    than by shelling out to `pdftotext`. Two reasons, both found the hard way:

    * An external binary is a dependency nothing declares. The test runner happened to have
      poppler installed and CI did not, so the guard silently changed behaviour between them.
    * `pdftotext` reports a file it cannot parse by exiting non-zero with empty output, which read
      as "no organisation named" — the one answer this module is not allowed to give when it has
      not actually looked. pdfplumber raises, and a raised exception is already a finding.

    Colours matter here as much as text, and more than they do in a deck. Converting a branded
    deck to PDF keeps the logo and the palette but does not necessarily carry the footer that
    names the bank: of the 95 PDFs in this repository's archive, 78 name the organisation in their
    text and the other 17 do not — while carrying its logo on the cover and its brand purple on
    every slide. A text-only check passed all 17.

    What is deliberately *not* checked is the presence of images. `no_embedded_images` catches a
    logo in a deck because a neutral deck embeds none — its charts are native shapes. In a PDF
    every chart is an image, so the same rule would flag every file, branded or not.
    """
    import pdfplumber                       # imported here: only PDFs pay the import cost

    wanted = {c.upper() for c in colours}
    text_parts: list[str] = []
    used: set[str] = set()
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            for obj in chain(page.chars, page.rects, page.lines, page.curves):
                for key in ("non_stroking_color", "stroking_color"):
                    hexed = _as_hex(obj.get(key))
                    if hexed in wanted:
                        used.add(hexed)
            page.flush_cache()              # a 30-slide deck otherwise holds every page in memory

    found = [f"names the organisation ({n})" for n in names if _mentions(" ".join(text_parts), n)]
    if used:
        found.append(f"uses brand colours {', '.join(sorted(used))}")
    return found


def _as_hex(value: Any) -> str | None:
    """A PDF colour as `RRGGBB`, whichever space it was written in.

    pdfplumber hands back what the content stream said: a single number for grayscale, three for
    RGB, four for CMYK. Rounded to 8 bits so that the value compares equal to the hex a designer
    wrote — a PDF draws the brand purple as `0.4352941176 0.0 0.7137254901 rg`, and that has to
    read as the same colour the theme names.
    """
    if value is None:
        return None
    parts = list(value) if isinstance(value, (list, tuple)) else [value]
    try:
        nums = [float(x) for x in parts]
    except (TypeError, ValueError):
        return None
    if len(nums) == 1:
        rgb = (nums[0], nums[0], nums[0])
    elif len(nums) == 3:
        rgb = (nums[0], nums[1], nums[2])
    elif len(nums) == 4:
        c, m, y, k = nums
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        return None
    return "".join(f"{max(0, min(255, round(v * 255))):02X}" for v in rgb)


def screen(paths: list[Path]) -> dict[str, list[str]]:
    """Inspect several artefacts. Returns only the ones with findings."""
    out: dict[str, list[str]] = {}
    for path in paths:
        findings = inspect(path)
        if findings:
            out[str(path)] = findings
    return out
