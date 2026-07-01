# Builder stage — installs uv, resolves dependencies, and builds the
# project. Build-time tooling (uv, pip) stays here and is not copied
# into the runtime image.
FROM python:3.14-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-emit-project --format requirements-txt \
         -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f /tmp/requirements.txt

# Runtime stage — only git (needed at runtime by the onboard fetcher)
# and the installed Python packages are copied from the builder.
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# ui/router.py reads DEPLOY_CONTRACT.md at IMPORT time. In the source tree
# src/robotsix_central_deploy/ui/DEPLOY_CONTRACT.md is a symlink to the
# canonical docs/DEPLOY_CONTRACT.md, so the built wheel ships a dangling link
# (docs/ is outside the package) and the app crash-loops on startup with
# FileNotFoundError. Copy the REAL file from the build context into the
# installed package location so the import succeeds.
COPY docs/DEPLOY_CONTRACT.md \
     /usr/local/lib/python3.14/site-packages/robotsix_central_deploy/ui/DEPLOY_CONTRACT.md

EXPOSE 8100

CMD ["robotsix-lifecycle"]
