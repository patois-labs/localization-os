# Localization OS TMS - production container image (Phase 4-C).
#
# Two-stage build: the "builder" stage compiles/collects the Python deps
# (including psycopg's build-time needs) into a virtualenv, then the final
# "runtime" stage copies only that venv + the app. Result: no compilers,
# no pip cache, and no build headers in the shipped image.
#
# Runs uvicorn DIRECTLY (no --reload - that is a dev-only loader; see run.py,
# which is NOT used here). The container binds 0.0.0.0:8000 internally; put it
# behind the reverse proxy in deploy/ for public traffic (see docs/DEPLOY.md).

# ---------------------------------------------------------------------------
# Base images. Every FROM below takes its image from one of these two ARGs,
# so the builder and runtime stages can never drift onto different Python bases. A PINNED
# value has the form  <name>:<tag>@sha256:<64 hex>  : Docker pulls by the digest (an exact,
# immutable image) and the tag stays in the value as the human-readable name of what it is.
# Digests are reviewed multi-platform indexes resolved on 2026-10-06.
#
# Bump digests with scripts/pin_base_images.py (registry HTTPS; no Docker needed):
#   .venv\Scripts\python.exe scripts\pin_base_images.py          (resolve + rewrite below)
#   .venv\Scripts\python.exe scripts\pin_base_images.py --check  (exit 1 while unpinned)
# See docs/DEPLOY.md, "Base image pins". tests/verify_docker_image.py holds the shape.
# ---------------------------------------------------------------------------
ARG PYTHON_BASE=python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
ARG TAILSCALE_BASE=tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065

# ---------------------------------------------------------------------------
# Stage 1 - builder: create a self-contained venv with all requirements.
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS builder

# Faster, quieter, deterministic pip/python behaviour.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps. psycopg[binary] ships prebuilt wheels (no libpq/compiler needed),
# but build-essential + libpq-dev are kept as a safety net so a source build of
# any dep still succeeds. They live ONLY in this stage and never reach runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Build everything into an isolated venv we can copy wholesale into the runtime.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.lock requirements-postgres.lock ./
# The DEFAULT `pip install -r requirements.txt` is copyleft-free on purpose (the LGPL
# psycopg + ldap3 drivers were moved to opt-in extras files for procurement "zero
# AGPL/GPL" scans). The CONTAINER/enterprise profile documents Postgres support
# (docs/embedded.md, docs/DEPLOY.md), so the image DELIBERATELY adds the LGPL Postgres
# driver back via requirements-postgres.lock; the bare `pip install -r requirements.txt`
# install does NOT include it. (LDAP and SAML stay opt-in even in the image: see the
# WITH_SAML / WITH_LDAP build arguments just below.)
RUN pip install --require-hashes --no-cache-dir --no-deps -r requirements.lock -r requirements-postgres.lock

# OPT-IN identity profiles, OFF by default. The default image installs exactly the two
# locks above and nothing else; these steps are no-ops unless the build passes
#   --build-arg WITH_SAML=1   SAML 2.0 SP: python3-saml + isodate + the native xmlsec
#                             binding (requirements-saml.lock). Binary wheels only for
#                             lxml/xmlsec, so no compiler is needed and a missing wheel
#                             fails the build instead of starting a source compile.
#   --build-arg WITH_LDAP=1   LDAP / Active Directory: ldap3 (LGPL, pure Python;
#                             requirements-ldap.lock).
# Both locks are ADDITIVE over requirements.lock (lxml and pyasn1 come from the base
# lock), so --no-deps keeps the no-new-resolution rule. Any value other than 0 or 1 fails.
# The runtime stage adds the xmlsec shared library and a smoke step (scripts/saml_smoke.py:
# imports, libxml2 compatibility, a real sign + verify) under the same argument. A hosted
# instance is built with WITH_SAML=1 (deploy/aws-hosted/LAUNCH.md); the default build omits it.
#
# FALLBACK, SAML only: --build-arg SAML_LXML=6.0.0 (with WITH_SAML=1) replaces the locked
# lxml with the 6.0.0 binary wheel, the pairing with xmlsec 1.3.17 proven on a small AWS
# host. Use it only when the SAML smoke fails with the locked lxml; the image then differs
# from requirements.lock by that one pin (record it). "locked" (default) changes nothing.
# KNOWN ADVISORY: lxml 6.0.0 is inside PYSEC-2026-87 (an XXE, fixed in lxml
# 6.1.0). The fallback therefore prints a
# WARNING into the build log (a warning only, it does not stop the build or the launch), and
# the operator guide asks for a pip-audit of the finished image logged in the change log. The
# pin stays in the automated audit as its own inventory (scripts/audit_runtime_profiles.py),
# where the recorded advisory is a time-limited, visible WARNING.
COPY requirements-saml.lock requirements-ldap.lock ./
COPY scripts/locks/requirements-saml-lxml.lock requirements-sqlcipher.lock ./
COPY scripts/locks/requirements-build.lock ./
ARG WITH_SAML=0
ARG WITH_LDAP=0
ARG SAML_LXML=locked
RUN case "$WITH_SAML" in \
        0) ;; \
        1) pip install --require-hashes --no-cache-dir --no-deps --only-binary lxml,xmlsec -r requirements-saml.lock ;; \
        *) echo "WITH_SAML must be 0 or 1, got '$WITH_SAML'" >&2; exit 1 ;; \
    esac
RUN case "$SAML_LXML" in \
        locked) ;; \
        6.0.0) [ "$WITH_SAML" = 1 ] || { echo "SAML_LXML=6.0.0 needs WITH_SAML=1" >&2; exit 1; }; \
            echo "WARNING: SAML_LXML=6.0.0 installs lxml 6.0.0, affected by PYSEC-2026-87" >&2; \
            echo "WARNING: (XXE, fixed in lxml 6.1.0). pip-audit this image and log the result" >&2; \
            echo "WARNING: in the change log (deploy/aws-hosted/LAUNCH.md, section 3)." >&2; \
            pip install --require-hashes --no-cache-dir --no-deps --only-binary lxml -r requirements-saml-lxml.lock ;; \
        *) echo "SAML_LXML must be locked or 6.0.0, got '$SAML_LXML'" >&2; exit 1 ;; \
    esac
RUN case "$WITH_LDAP" in \
        0) ;; \
        1) pip install --require-hashes --no-cache-dir --no-deps -r requirements-ldap.lock ;; \
        *) echo "WITH_LDAP must be 0 or 1, got '$WITH_LDAP'" >&2; exit 1 ;; \
    esac

ARG WITH_SQLCIPHER=0
RUN case "$WITH_SQLCIPHER" in \
        0) ;; \
        1) apt-get update \
            && apt-get install -y --no-install-recommends libsqlcipher-dev \
            && rm -rf /var/lib/apt/lists/* \
            && pip install --require-hashes --no-cache-dir --no-deps -r requirements-build.lock \
            && pip install --require-hashes --no-cache-dir --no-deps --no-build-isolation -r requirements-sqlcipher.lock ;; \
        *) echo "WITH_SQLCIPHER must be 0 or 1, got '$WITH_SQLCIPHER'" >&2; exit 1 ;; \
    esac

# ---------------------------------------------------------------------------
# Tailscale binaries (OPT-IN private egress to a model server on your own network).
# We COPY the static `tailscale` + `tailscaled` binaries out of the official
# image into our runtime below. They ship in EVERY build but do NOTHING unless
# TS_AUTHKEY is set at runtime (docker-entrypoint.sh gates the daemon on it), so
# the default container's behavior is byte-identical. Pinned to a specific tag
# (NOT :latest), and by digest once TAILSCALE_BASE at the top carries one. See
# docs/DEPLOY.md.
# ---------------------------------------------------------------------------
FROM ${TAILSCALE_BASE} AS tailscale

# ---------------------------------------------------------------------------
# Stage 2 - runtime: slim image with just the venv + app + a non-root user.
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Use the venv built in stage 1.
    PATH="/opt/venv/bin:$PATH" \
    # DB + uploads + keyfiles live here; docker-compose maps a named volume to it
    # so everything persists across container restarts/upgrades. See config.py:
    # LOC_TMS_DATA_DIR overrides the on-disk location of the data dir.
    LOC_TMS_DATA_DIR=/data \
    # Declare this a REAL server install. Without it, LOC_TMS_DATA_DIR=/data (set
    # above for the volume) would look identical to a verify-suite temp dir and the
    # container would boot in test mode - no login wall, no scheduler. With it set,
    # startup turns the login wall ON (but seeds NO dev accounts: first visit hits
    # /auth/setup to create the admin) and runs the scheduler. See app/config.py
    # (IS_SERVER / IS_TEST) and app/main._startup.
    LOC_TMS_SERVER=1 \
    # Bind on all interfaces INSIDE the container (the proxy fronts it).
    HOST=0.0.0.0 \
    PORT=8000

# libpq5 is the only runtime shared lib psycopg needs (the binary wheel links it).
# curl is used by the HEALTHCHECK below. fonts-noto-core + fonts-noto-cjk give the typst
# PDF writer (app/filters/pdf_filter.py) real glyph coverage for EVERY default locale:
# Noto core covers Latin/Cyrillic/Greek/Arabic/Hebrew/Devanagari/Bengali/Thai and Noto
# CJK covers Chinese/Japanese/Korean, so translated PDFs render correctly (with typst's
# shaping + fallback) instead of blank/tofu. Cost: ~fonts-noto-cjk adds ~110 MB to the
# image - the price of correct non-Latin delivery. Override/extend via LOC_TMS_PDF_FONT
# (family name) or LOC_TMS_PDF_FONT_DIR (extra font dir); see docs/embedded.md.
ARG APT_UPGRADE=0
RUN apt-get update \
    && if [ "$APT_UPGRADE" = 1 ]; then DEBIAN_FRONTEND=noninteractive apt-get upgrade -y; fi \
    && apt-get install -y --no-install-recommends libpq5 postgresql-client curl fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# SAML only (WITH_SAML=1, see the builder stage): the xmlsec1 OpenSSL crypto backend the
# python xmlsec binding loads at run time. On the Debian base this name may resolve to its
# t64-renamed provider; apt picks it. Nothing is installed in the default image.
ARG WITH_SAML=0
ARG WITH_LDAP=0
RUN if [ "$WITH_SAML" = 1 ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends libxmlsec1-openssl \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Copy the prebuilt virtualenv from the builder stage.
ARG WITH_SQLCIPHER=0
RUN if [ "$WITH_SQLCIPHER" = 1 ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends libsqlcipher1 \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=builder /opt/venv /opt/venv

# The running image never installs packages (the locks were installed in the builder), so
# pip is removed from both the base's system Python and the copied venv. pip 26.2.1, the
# newest release, still vendors msgpack 1.1.2, urllib3 2.7.0 and setuptools 70.3.0, which
# the image scan reports as fixable HIGH findings (GHSA-6v7p-g79w-8964, CVE-2026-97687,
# CVE-2026-97689, CVE-2025-47273) that no pip release can clear yet. Removing pip also drops
# an installer from the attack surface. Re-add it only in a builder stage. KEEP_PIP=1 is for
# the CI release-image job ONLY (it runs `pip check` / `pip freeze` and builds profile layers
# over this image); never set it for an image that serves anyone. CI also builds the default
# image and asserts pip is absent.
ARG KEEP_PIP=0
RUN if [ "$KEEP_PIP" = 0 ]; then \
        rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
            /opt/venv/lib/python3.13/site-packages/pip /opt/venv/lib/python3.13/site-packages/pip-*.dist-info \
            /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
            /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13; \
    elif [ "$KEEP_PIP" != 1 ]; then echo "KEEP_PIP must be 0 or 1, got '$KEEP_PIP'" >&2; exit 1; \
    fi

# OPT-IN Tailscale sidecar binaries (static, no extra runtime libs). Present in
# every image but inert unless TS_AUTHKEY is set at runtime - docker-entrypoint.sh
# starts tailscaled ONLY then. Without the key, not one new process runs and the
# container is byte-identical to before. State lives on the /data volume so a
# re-join is only needed if the ephemeral node is reaped (see the entrypoint).
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale

# Create an unprivileged user and a data dir it owns (the volume mount inherits
# these perms on first create). Running as non-root is a hard requirement.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data

WORKDIR /app

# Copy the application. .dockerignore is an ALLOW-LIST: only the app, its
# migrations, assets, entrypoint, locks and license files are sent to the builder.
# The code is owned by ROOT, not by the app user: uvicorn runs as `app`, so a
# file-write bug in the app can never rewrite the app's own code (or the audit verifier it
# runs) and persist across restarts. Only /data (above) is writable by `app`.
COPY . /app
ARG RELEASE=""
ARG RELEASE_COMMIT=""
RUN printf '%s\n' "$RELEASE" > /app/RELEASE \
    && printf '%s\n' "$RELEASE_COMMIT" > /app/RELEASE_COMMIT

# The entrypoint runs `alembic upgrade head` first when DATABASE_URL points at
# Postgres, then execs uvicorn. Ensure it is executable even if git on the build
# host could not preserve the +x bit (Windows). `go-w` strips group/other write bits
# that a permissive checkout umask could carry in, so /app stays read-only to `app`.
RUN chmod +x /app/docker-entrypoint.sh \
    && chmod -R go-w /app

USER app

# Build-time proof of the ownership split, run as the app user: the build
# FAILS if the app user cannot write /data or can write anything under /app.
RUN test -w /data \
    && test -z "$(find /app -writable -print -quit)" \
    || { echo "image check FAILED: /app must be read-only and /data writable for user app" >&2; \
         exit 1; }

# Opt-in profile smoke test, run as the app user: a SAML or LDAP image that cannot use
# its library FAILS THE BUILD here instead of failing the boot capability check later.
# scripts/saml_smoke.py imports the stack, requires lxml and xmlsec to share the libxml2
# major.minor (python-xmlsec's own rule), then signs and verifies a small SAML document
# with a throwaway in-memory key and checks that a tampered copy is rejected.
RUN if [ "$WITH_SAML" = 1 ]; then \
        python /app/scripts/saml_smoke.py; \
    fi \
    && if [ "$WITH_LDAP" = 1 ]; then \
        python -c "import ldap3; print('LDAP smoke: ldap3', ldap3.__version__)"; \
    fi

EXPOSE 8000

# PROCESS-LIVENESS probe only. /health is unauthenticated, touches no database and never
# enters the shared worker threadpool, so it answers 200 while the app is saturated - it
# tells you the interpreter is alive, NOT that this container can serve. Deliberately kept
# here (a container supervisor should restart a DEAD process, not a busy one); the
# can-it-serve check is /ready, which is what a load balancer or orchestrator should poll.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Default command: production uvicorn, NO reload. The entrypoint execs this.
#
# Proxy trust is resolved by the application from trusted_proxy_ips at startup.
# Invoke uvicorn's scheme parser inside the Host boundary, preserving socket identity.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
