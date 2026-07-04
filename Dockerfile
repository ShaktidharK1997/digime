FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so code changes don't bust the layer cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Socket Mode: outbound connection only, no ports to expose.
# Mount ./config (tokens + memory.db) and credentials.json at runtime —
# see docker-compose.yml.
CMD ["uv", "run", "--frozen", "--no-sync", "python", "main.py"]
