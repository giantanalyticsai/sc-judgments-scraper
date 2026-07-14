# syntax=docker/dockerfile:1
#
# Local Docker image for the SC judgments scraper.
# Multi-stage: the builder resolves deps with uv and bakes the ~95 MB captcha
# ONNX model into the image so a run needs no GitHub access. The runtime stage
# is a slim, non-root image containing just the venv, source, and model.
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
# which the scheduler relies on for the trigger time and "today".
# gosu lets the entrypoint drop from root to an unprivileged user cleanly
# (proper signal/TTY handling, unlike su/sudo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Bring over the built venv, source, and baked-in model.
COPY --from=builder /app /app

# Create the unprivileged runtime user and give it the app + data dir. The
# container still STARTS as root so the entrypoint can make a freshly-mounted
# data volume writable (see docker-entrypoint.sh); it drops to `runner` via
# gosu before running any application code. This is what makes the image
# portable across a laptop bind mount, ECS, or any other host.
RUN useradd -m -u 1000 runner \
    && mkdir -p /app/data \
    && chown -R runner:runner /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]

# The entrypoint does the root-only setup then drops to `runner` and execs the
# command. Args after the image name are passed to scrape.py, e.g.
# `docker run sc-scraper --daily`. The scheduler service reuses this same
# entrypoint but overrides the command with schedule.sh (see docker-compose.yml).
ENTRYPOINT ["docker-entrypoint.sh", "python", "scrape.py"]
CMD ["--help"]
