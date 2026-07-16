#!/bin/sh
# The container runs unprivileged from PID 1 (USER 1000 in the Dockerfile) — it
# is never root, so there is no privilege drop and no gosu. This entrypoint just
# ensures the (optional) local data dir exists, then execs the command.
#
# On Fargate, output goes to S3, so DATA_DIR is unused. For a local bind mount,
# the mounted ./data must already be writable by UID 1000 — the container can no
# longer chown it (by design: no root, ever).
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true

exec "$@"
