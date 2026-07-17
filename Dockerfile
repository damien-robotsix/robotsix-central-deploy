# syntax=docker/dockerfile:1
# Digest-pinned python:3.14-slim, same base in both stages (docker standard).
ARG BASE_DIGEST=sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

# Builder stage — uv and git resolve the frozen lockfile (including the
# git-pinned first-party deps) and install the project. Build tooling stays
# here; only installed packages are copied into the runtime image.
FROM python:3.14-slim@${BASE_DIGEST} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

WORKDIR /app

# hadolint ignore=DL3008 — version pinning fragile across Debian releases (Bookworm→Trixie); base image digest is already pinned for reproducibility.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock _mill_build.py ./
COPY src/ ./src/
# src/robotsix_central_deploy/ui/DEPLOY_CONTRACT.md is a symlink to
# ../../../docs/ui/DEPLOY_CONTRACT.md; the canonical file must exist in the
# build stage or hatchling fails to resolve it when building the wheel.
COPY docs/ui/DEPLOY_CONTRACT.md ./docs/ui/DEPLOY_CONTRACT.md

RUN --mount=type=secret,id=github_token,required=false \
    if [ -f /run/secrets/github_token ]; then \
      GITHUB_TOKEN=$(cat /run/secrets/github_token) && \
      git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; \
    fi && \
    uv export --frozen --no-emit-project --format requirements-txt \
         -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f /tmp/requirements.txt

# Runtime stage — only git (needed at runtime by the onboard fetcher), the
# installed Python packages, and the console script come from the builder.
FROM python:3.14-slim@${BASE_DIGEST} AS production

WORKDIR /app

# hadolint ignore=DL3008 — version pinning fragile across Debian releases (Bookworm→Trixie); base image digest is already pinned for reproducibility.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/robotsix-lifecycle /usr/local/bin/robotsix-lifecycle

# Non-root runtime user. /data is the state-volume mount point; a named
# volume created empty inherits this ownership on first use (pre-existing
# root-owned volumes need a one-time chown — see docs/deployment.md).
# /app (the workdir) must also be writable: default state paths
# (secrets.key, lifecycle_state.yaml, …) are relative when the
# ROBOTSIX_LIFECYCLE_*_PATH variables are unset.
RUN useradd -u 1000 -m app && mkdir /data && chown app:app /data /app
USER app

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/health').status==200 else 1)"

CMD ["robotsix-lifecycle"]
