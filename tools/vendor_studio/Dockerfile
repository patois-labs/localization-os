# Vendor Studio - standalone container image (deployed separately from the main app).
#
# Two-stage build mirroring the main app's Dockerfile conventions: a builder stage
# compiles the venv, the runtime stage copies only the venv + this tool's code and
# runs as a non-root user. The image ships ONLY tools/vendor_studio (build context is
# this directory), so it never imports the Localization OS app - the wire-only isolation is
# structural at the image level too.
#
# LF line endings on this file + the entrypoint are pinned in .gitattributes (the
# 2026-07-20 CRLF crash-loop lesson): a Docker build ships the working tree, so a CRLF
# shebang would ENOENT-crash the container.

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
COPY requirements.lock .
RUN pip install --require-hashes --no-cache-dir --no-deps --only-binary=:all: -r requirements.lock

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # SQLite DB lives on the mounted volume.
    VSTUDIO_DB_PATH=/data/vendor_studio.db \
    VSTUDIO_HOST=0.0.0.0 \
    VSTUDIO_PORT=8100
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
# No installer in the running image (pip 26.2.1 vendors msgpack, urllib3 and setuptools
# versions the image scan flags as fixable HIGH); same fix as the main Dockerfile.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
        /opt/venv/lib/python3.13/site-packages/pip /opt/venv/lib/python3.13/site-packages/pip-*.dist-info \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
        /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown -R app:app /data
WORKDIR /app
COPY . /app
# Runtime code stays root-owned, even if the checkout has group-write bits.
RUN chmod -R go-w /app
USER app
RUN test -w /data && test -z "$(find /app -writable -print -quit)" || \
    { echo "image check FAILED: /app must be read-only and /data writable for user app" >&2; exit 1; }
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/health || exit 1
# run.py adds the package dir to sys.path and starts uvicorn on VSTUDIO_HOST:VSTUDIO_PORT.
CMD ["python", "run.py"]
