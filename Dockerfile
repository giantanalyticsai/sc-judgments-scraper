# syntax=docker/dockerfile:1
#
# Local Docker image for the SC judgments scraper.
# Multi-stage: the builder resolves deps with uv and bakes the ~95 MB captcha
# ONNX model into the image so a run needs no GitHub access. The runtime stage
# is a slim image that runs as an unprivileged user (UID 1000) from PID 1 —
# never root — containing just the venv, source, and model.
#
# Build:  docker build -t sc-scraper .
# Run:    docker run --rm -v "$PWD/data:/app/data" \
#             sc-scraper --start-date 2024-01-02 --end-date 2024-01-03

# ---- builder: install deps + prefetch captcha model -------------------------
FROM python:3.12-slim AS builder

# uv (Astral) for fast, lockfile-reproducible dependency installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Install deps first (cached layer) using only the manifest + lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the source, then prefetch + hash-verify the captcha model so it ships
# inside the image (see src/captcha/model.py).
COPY . .
RUN uv run python fetch_model.py

# ---- runtime: slim, non-root ------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

# tzdata so TZ (e.g. Asia/Kolkata) is honoured by both Python and `date`,
# which decide which day "today"/"yesterday" means. No gosu / privilege-drop
# tooling: the container runs unprivileged from PID 1 (see USER below), so it
# is never root at any point.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/runner

# Bring over the built venv, source, and baked-in model.
COPY --from=builder /app /app

# Create the unprivileged runtime user, own the app (venv + source + baked
# model + data dir), then switch to it. The image NEVER runs as root: PID 1 is
# already UID 1000, so a compromise can't start from root. On Fargate output
# goes to S3 (no local writes); for a local bind mount the operator must make
# ./data writable by UID 1000 (the container can no longer chown it, by design).
RUN useradd -m -u 1000 runner \
    && mkdir -p /app/data \
    && chown -R runner:runner /app
COPY --chown=runner:runner docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER 1000:1000
VOLUME ["/app/data"]

# Entrypoint runs as the unprivileged user and execs the command. Args after
# the image name are passed to scrape.py, e.g. `docker run sc-scraper --daily`.
# The scheduler service reuses this same entrypoint but overrides the command
# with schedule.sh (see docker-compose.yml).
ENTRYPOINT ["docker-entrypoint.sh", "python", "scrape.py"]
CMD ["--help"]
