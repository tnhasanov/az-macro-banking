# The monitor and everything it needs to turn a CBA spreadsheet into a board deck.
#
# LibreOffice is the reason this image is not small: PDF conversion needs a real renderer, and a deck
# that exists only as PPTX is half a deliverable. Poppler provides the page rasteriser for slide
# previews, and Tesseract is the OCR fallback for the occasional scanned publication - its output is
# always marked unverified and never becomes a figure, but it makes a scanned page searchable.
#
# Built for a scheduled, non-interactive run: no shell entry point, no root at runtime, and a
# writable volume for the only two directories that hold state.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Baku \
    AZMONITOR_DATA_DIR=/var/lib/azmonitor/data \
    AZMONITOR_OUTPUT_DIR=/var/lib/azmonitor/outputs

# libreoffice-impress: PPTX -> PDF.  poppler-utils: page images.  tesseract: OCR fallback.
# fonts-open-sans + fonts-dejavu: the deck's typeface, and a fallback with full Azerbaijani coverage
# (ə, ğ, ı, ö, ş, ü render as boxes without them, which is worse than an ugly font).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        poppler-utils \
        tesseract-ocr tesseract-ocr-aze tesseract-ocr-eng \
        fonts-open-sans fonts-dejavu-core \
        ca-certificates tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --home-dir /home/azmonitor --shell /usr/sbin/nologin azmonitor

WORKDIR /app

# Dependencies first, so a change to the code does not rebuild the world.
COPY pyproject.toml README.md ./
COPY azmonitor/__init__.py ./azmonitor/__init__.py
RUN pip install --upgrade pip && pip install -e . && pip cache purge || true

COPY azmonitor ./azmonitor
COPY config ./config
COPY theme ./theme
COPY scripts ./scripts
COPY narratives ./narratives

RUN mkdir -p /var/lib/azmonitor/data /var/lib/azmonitor/outputs \
    && chown -R azmonitor:azmonitor /var/lib/azmonitor /app \
    && chmod +x scripts/*.sh

USER azmonitor

# LibreOffice writes a profile on first run; without a writable HOME it fails on a read-only root.
ENV HOME=/home/azmonitor

# The image is a tool, not a service: it runs one task and exits. The timer decides which.
ENTRYPOINT ["python", "-m", "azmonitor.cli"]
CMD ["schedule", "status"]

# A container that cannot load its own configuration is broken whatever else works.
HEALTHCHECK --interval=12h --timeout=30s --start-period=10s --retries=2 \
    CMD python -c "from azmonitor import config; config.settings(); config.schedule_config(); config.delivery_config()" || exit 1
