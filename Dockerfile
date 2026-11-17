# ==============================================================================
# CypherSOL Production Multi-Stage Containerfile
# Unified Bank Statement Analyzer & Forensic Fund Trail Intelligence Platform
# Compatible with: Docker, Render, Google Cloud Run, AWS App Runner/ECS, Azure Container Apps
# ==============================================================================

FROM python:3.12-slim-bookworm AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install essential system utilities for PDF decryption, rendering, and OCR
# - qpdf: AES-256 PDF decryption and linearization
# - poppler-utils: pdftoppm for rasterizing PDF pages
# - tesseract-ocr: Multilingual OCR engine (English and Hindi)
# - libmagic1: Accurate MIME type identification
# - curl & ca-certificates: Healthcheck and secure certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    qpdf \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-hin \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip and install build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install all performance accelerators and python libraries
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code, UI assets, and sample statements
COPY src/ ./src/
COPY sample_statements/ ./sample_statements/
COPY README.md ./

# Install CypherSOL package
RUN pip install --no-cache-dir .

# Create non-root user and prepare persistent data directory
RUN useradd -u 10001 -m -s /bin/bash cyphersol && \
    mkdir -p /data/cases /data/logs && \
    chown -R cyphersol:cyphersol /app /data && \
    chmod 700 /data

# Switch to unprivileged user
USER cyphersol

# Standard runtime environment variables
ENV HOST=0.0.0.0 \
    PORT=8080 \
    CYPHERSOL_DATA_DIR=/data \
    CYPHERSOL_DB_PATH=/data/cyphersol_vault.db \
    CYPHERSOL_ALLOWED_HOSTS="*" \
    CYPHERSOL_NO_BROWSER=1

# Expose default HTTP port
EXPOSE 8080

# Persistent volume for vault DB, original input documents, generated reports, and rotating logs
VOLUME ["/data"]

# HTTP healthcheck endpoint for container orchestrators (Render, Cloud Run, AWS, Azure, K8s)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/api/health" || exit 1

# Launch CypherSOL server
ENTRYPOINT ["python3", "-m", "cyphersol.cli"]
CMD ["--host", "0.0.0.0", "--no-browser"]
