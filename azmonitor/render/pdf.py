"""PDF rendering with LibreOffice and slide previews with pdftoppm.

LibreOffice is run headless with a private user profile. Some sandboxes block AF_UNIX
sockets; a small LD_PRELOAD shim is compiled on demand (requires gcc) in that case.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

_SHIM_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <sys/socket.h>
#include <errno.h>
static int (*real_socket)(int, int, int) = 0;
int socket(int domain, int type, int protocol) {
    if (!real_socket) real_socket = dlsym(RTLD_NEXT, "socket");
    if (domain == AF_UNIX) { errno = EACCES; return -1; }
    return real_socket(domain, type, protocol);
}
"""


def _needs_shim() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.close()
        return False
    except OSError:
        return True


def _ensure_shim() -> Path | None:
    so = Path(tempfile.gettempdir()) / "lo_socket_shim.so"
    if so.exists():
        return so
    if not shutil.which("gcc"):
        return None
    src = Path(tempfile.gettempdir()) / "lo_socket_shim.c"
    src.write_text(_SHIM_SOURCE, encoding="utf-8")
    r = subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(so), str(src), "-ldl"], capture_output=True)
    return so if r.returncode == 0 else None


def soffice_available() -> bool:
    return shutil.which("soffice") is not None


def convert_to_pdf(pptx_path: Path, out_dir: Path | None = None, timeout: int = 300) -> Path:
    if not soffice_available():
        raise RuntimeError("LibreOffice (soffice) is not installed; PDF rendering unavailable")
    pptx_path = Path(pptx_path).resolve()
    out_dir = Path(out_dir or pptx_path.parent).resolve()
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"
    if _needs_shim():
        shim = _ensure_shim()
        if shim:
            env["LD_PRELOAD"] = str(shim)
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as prof:
        cmd = ["soffice", f"-env:UserInstallation={Path(prof).as_uri()}", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(pptx_path)]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    pdf = out_dir / (pptx_path.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"PDF conversion failed: {r.stderr[-500:] if r.stderr else r.stdout[-500:]}")
    return pdf


def previews(pdf_path: Path, out_dir: Path, dpi: int = 70, fmt: str = "png") -> list[Path]:
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm (poppler-utils) is not installed; previews unavailable")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("slide-*"):
        old.unlink()
    subprocess.run(["pdftoppm", f"-{fmt}", "-r", str(dpi), str(pdf_path), str(out_dir / "slide")], check=True, capture_output=True, timeout=300)
    return sorted(out_dir.glob(f"slide-*.{fmt}"))
