FROM python:3.11-slim

WORKDIR /app

# Install build backend
RUN pip install --no-cache-dir setuptools

# Copy package metadata and source first (layer-cache friendly)
COPY pyproject.toml ./
COPY src/ ./src/

# Install the package and its runtime dependencies
RUN pip install --no-cache-dir .

# Copy the component registry config (expected at /app/config/components.yaml
# per ROBOTSIX_LIFECYCLE_REGISTRY_PATH in docker-compose.yml)
COPY config/ ./config/

EXPOSE 8100

CMD ["robotsix-lifecycle"]
