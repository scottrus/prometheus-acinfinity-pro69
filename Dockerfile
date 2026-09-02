# syntax=docker/dockerfile:1

# Chainguard images are used for their near-zero CVE count. The free public tier
# publishes only `latest` and `latest-dev`, with no version tags, so both stages
# are pinned by digest instead. Dependabot updates these.
#
# Re-resolve a digest by hand with:
#   crane digest cgr.dev/chainguard/python:latest

# --- build ------------------------------------------------------------------
# The -dev variant carries pip and a shell; the runtime variant carries neither.
FROM cgr.dev/chainguard/python:latest-dev@sha256:3472bbd8c7a7fe9254dcccdbd8eefb8edf603d0882aa4ed527f8497957b10f5f AS build

# Chainguard's -dev variants default to the nonroot user, so a write to / is
# denied. Root for the build only; this stage is discarded.
USER root

WORKDIR /src

# Build into a venv so the runtime stage copies exactly one self-contained
# directory. The runtime image has no pip to install with.
#
# The venv path must be identical in both stages: a venv bakes its own absolute
# path into the console-script shebangs.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# setuptools-scm derives the version from git, and the build context has no
# .git. The release workflow passes the tag; a local build gets 0.0.0, so a
# locally built image reports a placeholder rather than a release number.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

RUN pip install --no-cache-dir .

# Drop back to nonroot so this stage does not end as root (hadolint DL3002).
USER nonroot

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:27c8d0e215a516e6d366549d1673baf6c42784277afcc8d904715a73982b9ba4

LABEL org.opencontainers.image.title="prometheus-acinfinity-pro69" \
      org.opencontainers.image.description="Prometheus exporter for the AC Infinity UIS Controller 69 PRO, read from the vendor cloud API" \
      org.opencontainers.image.source="https://github.com/scottrus/prometheus-acinfinity-pro69" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/python:latest"

COPY --from=build /venv /venv

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Chainguard runtime images already default to the nonroot user (65532), which
# matches runAsUser in the Helm chart. Stated explicitly so it survives a base
# image change.
USER 65532:65532

EXPOSE 8000

# Exec form keeps the exporter as PID 1, so SIGTERM reaches it directly. The
# poll loop waits on an Event so that signal is honoured at once.
#
# The backfill is the same image with a different entrypoint:
#   docker run --entrypoint acinfinity-backfill ...
ENTRYPOINT ["acinfinity-exporter"]

# No shell in this image, so the check is an exec-form python one-liner.
#
# /health is a REAL route that 404s unknown paths, which is what makes this
# check mean something. It does NOT assert collection success: a vendor outage
# must surface as acinfinity_collection_success 0 on a scrape somebody can
# alert on, not as a restart loop that destroys the evidence.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]
