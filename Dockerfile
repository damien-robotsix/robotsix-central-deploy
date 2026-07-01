FROM python:3.14-slim

WORKDIR /app

# Install git — the onboard fetcher shells out to `git` to fetch a target
# repo's docker-compose for preflight/confirm. Without it, onboard 500s.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata (incl. the lockfile) and source first (cache-friendly)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install via uv from the frozen lockfile. A plain `pip install .` cannot
# resolve transitive git dependencies (robotsix-board-agent → robotsix-llmio):
# those are declared in [tool.uv.sources], which pip does not read, so it falls
# back to PyPI and fails ("No matching distribution found for robotsix-llmio").
# uv reads the lock, resolves every (transitive) git source, and installs them.
RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-emit-project --format requirements-txt \
         -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f /tmp/requirements.txt


EXPOSE 8100

CMD ["robotsix-lifecycle"]
