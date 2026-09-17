# syntax=docker/dockerfile:1

# =============================================================================
# Card Reader — production image
#
# WHY MULTI-STAGE: compiling Python wheels needs gcc, make, and header packages
# — roughly 400 MB of toolchain. None of it is needed to RUN the app. A single
# stage would ship all of it to production, which is both wasted bytes and
# wasted attack surface: a compiler inside a running container is the first
# thing an attacker who gets code execution looks for. Building in one stage
# and copying only the finished virtualenv into a clean second stage means the
# toolchain exists at build time and simply does not exist at run time.
#
# WHY python:3.13-slim AND NOT alpine: this project was developed and verified
# on 3.13.2, so the runtime matches the dev environment exactly. `slim` is
# Debian, which means glibc, which means pip installs the prebuilt manylinux
# wheels for Pillow, pillow-heif and cryptography. Alpine uses musl, for which
# those wheels do not exist — pip would fall back to building each from source,
# turning a 40-second build into a multi-minute one and requiring libjpeg,
# libheif, zlib and OpenSSL dev headers to be installed by hand. The image
# would be smaller; nothing else about it would be better.
# =============================================================================


# -----------------------------------------------------------------------------
# Stage 1 — builder. Everything here is thrown away except /opt/venv.
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# PIP_NO_CACHE_DIR: pip's download cache is useless in a layer that is about to
# be discarded, and it would otherwise add ~100 MB to the copied venv's parent.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Present as a fallback only. Every pin in requirements.txt currently resolves
# to a manylinux wheel, so nothing actually compiles — but if a future pin has
# no wheel for this platform, the build still succeeds here instead of failing
# with a wall of C errors. It costs nothing, because this stage is discarded.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the system site-packages, because it makes the
# hand-off to stage 2 a single self-contained directory copy. Copying scattered
# files out of /usr/local would drag along pip, setuptools and their metadata.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt is copied ALONE, before the source. Docker caches layers by
# content: application code changes on every commit, dependencies change maybe
# monthly. Ordering it this way means an ordinary code edit reuses the cached
# dependency layer and rebuilds in seconds. Copying the whole project first
# would invalidate that layer on every single edit and reinstall all ten
# packages every time.
COPY requirements.txt .
RUN pip install --require-hashes=false -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2 — runtime. Clean base; no compiler, no pip cache, no build headers.
# -----------------------------------------------------------------------------
FROM python:3.13-slim

# NON-ROOT. A container process running as root that escapes its namespace —
# via a kernel bug or a careless `-v /:/host` — is root on the Azure VM. This
# app has no reason to need it: it binds port 8000 (above 1024, so no
# CAP_NET_BIND_SERVICE), writes only to /app/data, and installs nothing at run
# time. A fixed high UID rather than whatever useradd picks, so that files on a
# mounted volume have a predictable, stable owner across image rebuilds.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

# The entire dependency set, as one directory, with none of the tooling that
# built it. No apt packages are needed at run time: the Pillow and pillow-heif
# wheels vendor their own libjpeg/libheif, and cryptography vendors OpenSSL.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    # Unbuffered: Python otherwise block-buffers stdout when it is a pipe
    # rather than a terminal, so `docker logs` shows NOTHING until 4 KB has
    # accumulated. During a 170-second-per-card batch that reads as a hung
    # container when it is working perfectly.
    PYTHONUNBUFFERED=1 \
    # No .pyc files: the image layer is read-only, so they would be written
    # once per container start and never reused.
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied individually rather than `COPY . .`, so that adding a stray file to
# the project root — a .pem, a database dump, a scratch notebook — cannot
# silently end up inside a shipped image. .dockerignore is the second line of
# defence; this is the first.
COPY --chown=app:app app    ./app
COPY --chown=app:app static ./static
# The stub model server ships too, deliberately. It is how you prove the
# container's own pipeline works end to end when the real model endpoint is
# unreachable — which is the single most useful thing to be able to do while
# debugging a fresh deployment.
COPY --chown=app:app tools  ./tools

# The mount point for persistent state (SQLite database + retained card
# images). Created and chowned in the image so that when Docker mounts a fresh
# named volume here, the directory already has the right owner — otherwise the
# volume is created root-owned and the non-root app cannot write to it, which
# surfaces as a confusing "unable to open database file" on first boot.
#
# Note there is deliberately no VOLUME instruction: it would force an anonymous
# volume on anyone who ran the image without one, and those accumulate
# invisibly on the host. The named volume is declared in docker-compose.yml,
# where it is visible.
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 8000

# Uses /health, which deliberately does NOT call the model — it answers in
# milliseconds. Pointing a healthcheck at anything that touches the VLM would
# take 170 seconds per probe and Docker would kill a perfectly healthy
# container for failing a check it could never pass.
#
# Written with urllib rather than curl because curl is not in the slim image,
# and installing a whole HTTP client just to poll your own port is silly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# WHY sh -c: Azure App Service and Container Apps inject the port they expect
# the app to listen on as $PORT, which exec-form CMD cannot expand. `exec`
# replaces the shell with uvicorn so that uvicorn becomes PID 1 and receives
# SIGTERM directly — without it the shell holds PID 1, swallows the signal,
# and every `docker stop` waits the full 10-second timeout before a SIGKILL
# tears the process down mid-batch.
#
# WEB_CONCURRENCY defaults to 1 and MUST stay 1 until the SQLite store lands:
# job state currently lives in a module-level dict, so worker 2 has never heard
# of the job worker 1 just created and every poll is a coin-flip 404.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1}"]
