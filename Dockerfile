# syntax=docker/dockerfile:1
#
# catalog-sync as a container: `docker run ... schedule PROFILE --every 6h` is the intended use.
#
#   /data                          the workspace (state, cache, audit log, credentials): a volume
#   /config/catalog-sync.toml      the config: mounted read-only, never baked in
#
# There is no browser in the image (no `browser` extra): a headed login cannot run in a container.
# Log in on the host with the same workspace mounted, or with `login --form` / `--paste-curl`
# (see README, `schedule`). No secret is copied in: the build context is cut down by
# .dockerignore and this file copies only the files named below.
#
# Both base images are pinned by the digest of their multi-architecture index, so a rebuild gets
# the same bytes. To update: `docker buildx imagetools inspect python:3.12-slim` (and the uv tag),
# then replace the digests.

ARG PYTHON_BASE=python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

# --- builder: resolves the locked dependencies and installs the package into a venv -----------
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_BASE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
# The venv must live at the path it will have in the runtime image: its scripts name that path.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes, not on every edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-install-project

# The package itself, installed as a wheel (not editable): the runtime image has no source tree.
# README.md and LICENSE are read by the build backend (pyproject: readme, license-files).
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-editable

# --- runtime: the venv and nothing else ----------------------------------------------------------
FROM ${PYTHON_BASE} AS runtime

LABEL org.opencontainers.image.title="catalog-sync" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 catalog \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin catalog \
 && install -d -o catalog -g catalog -m 0700 /data \
 && install -d -m 0755 /config

COPY --from=builder /app/.venv /app/.venv

WORKDIR /app
# The tool reads ./catalog-sync.toml when --config is not given. A symlink to the mount point
# makes that the config of the container without a flag on every command; with nothing mounted
# it dangles, which the tool treats as "no config file" (so `--version` and `mock serve` work).
RUN ln -s /config/catalog-sync.toml /app/catalog-sync.toml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CATALOG_SYNC_HOME=/data

# Numeric, so that an orchestrator can verify "runs as non-root" without reading /etc/passwd.
USER 10001:10001
VOLUME ["/data"]

ENTRYPOINT ["catalog-sync"]
CMD ["--help"]

# Healthy = a scheduled cycle finished successfully within twice its interval (`health --help`).
# The start period must cover the first cycle; it is not meant for containers that run anything
# other than `schedule` (start those with --no-healthcheck).
HEALTHCHECK --interval=60s --timeout=15s --start-period=5m --retries=3 \
    CMD ["catalog-sync", "health"]
