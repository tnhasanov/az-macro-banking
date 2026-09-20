#!/usr/bin/env bash
# Environment setup for the Azerbaijan Macro & Banking Monitor (Debian/Ubuntu).
# Installs system packages for PDF rendering and previews, Python dependencies, and a
# fontconfig alias so LibreOffice renders the deck's Segoe UI with a metric-similar font.
# Idempotent; safe to re-run. Use --quiet to reduce output, --no-apt to skip system packages.
set -euo pipefail
cd "$(dirname "$0")/.."
QUIET=""; NOAPT=""
for a in "$@"; do case "$a" in --quiet) QUIET=1;; --no-apt) NOAPT=1;; esac; done
log() { [ -n "$QUIET" ] || echo "[setup] $*"; }

if [ -z "$NOAPT" ] && command -v apt-get >/dev/null 2>&1; then
  if ! command -v soffice >/dev/null 2>&1 || ! command -v pdftoppm >/dev/null 2>&1 || [ ! -f /usr/lib/libreoffice/program/libsdlo.so ]; then
    log "installing LibreOffice Impress, poppler-utils and fonts (sudo may be required)"
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    $SUDO apt-get update -qq || true
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq libreoffice-impress poppler-utils fonts-open-sans fonts-noto-core fonts-dejavu gcc >/dev/null 2>&1 || \
      log "WARNING: apt install failed; PDF rendering may be unavailable"
  fi
fi

log "installing Python package and dependencies"
python3 -m pip install -q --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install -q -e ".[dev]"
# the Debian 'cryptography' build can lack _cffi_backend, which breaks pdfplumber; refresh it if so
python3 -c "import cryptography.hazmat.bindings._rust" 2>/dev/null || python3 -m pip install -q --ignore-installed cffi cryptography >/dev/null 2>&1 || true

# fontconfig alias: Segoe UI -> Open Sans for LibreOffice rendering on Linux
FC_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fontconfig"
mkdir -p "$FC_DIR"
if ! grep -q "Segoe UI" "$FC_DIR/fonts.conf" 2>/dev/null; then
  cat > "$FC_DIR/fonts.conf" <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias><family>Segoe UI</family><prefer><family>Open Sans</family><family>Noto Sans</family><family>DejaVu Sans</family></prefer></alias>
  <alias><family>Segoe UI Semibold</family><prefer><family>Open Sans</family><family>Noto Sans</family><family>DejaVu Sans</family></prefer></alias>
</fontconfig>
EOF
  command -v fc-cache >/dev/null 2>&1 && fc-cache -f >/dev/null 2>&1 || true
fi

mkdir -p data outputs
log "done. Try: python -m azmonitor.cli status"
