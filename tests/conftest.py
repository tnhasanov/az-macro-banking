import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def tmp_data_env(tmp_path, monkeypatch):
    """Point the persistent data/output directories at a temporary location."""
    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZMONITOR_OUTPUT_DIR", str(tmp_path / "outputs"))
    return tmp_path


def minimal_pdf(path: Path, *, text: str = "", colour: str | None = None,
                image: bool = False) -> Path:
    """The smallest thing that is genuinely a PDF, carrying whichever marker a test needs.

    Written by hand rather than rendered, because the artefact guard reads a PDF for real: a
    `b"%PDF-1.7\\n"` stub is refused as uninspectable, which is correct behaviour and would mask
    whatever a test using one is actually about. Text is drawn with a standard font and colours
    are written into the content stream, so both reach the guard the way a converted deck's do.
    """
    body = []
    if colour:
        r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4))
        body.append(f"q {r:.6f} {g:.6f} {b:.6f} rg 10 10 80 40 re f Q")
    if image:
        # An inline 1x1 image, scaled up. A PDF carries its charts as images, so the guard must
        # not treat one as a marker — this is the file that proves it does not.
        body.append("q 40 0 0 40 100 20 cm BI /W 1 /H 1 /CS /RGB /BPC 8 /F /AHx ID 7f7f7f> EI Q")
    if text:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        body.append(f"BT /F1 14 Tf 20 150 Td ({escaped}) Tj ET")
    stream = "\n".join(body).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start_xref}\n".encode()
    out += b"%%EOF\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path
