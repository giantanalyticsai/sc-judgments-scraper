#!/bin/sh
# Portable entrypoint: works identically on a laptop bind mount, an ECS task,
# or any other host — regardless of who owns the mounted data directory.
#
# The problem it solves: when Docker bind-mounts a host path (or an EFS volume)
# the directory arrives owned by whatever the host/orchestrator decided (often
# root, or an arbitrary UID). A non-root app then can't write to it. Rather than
# demand the operator fix ownership out-of-band, we do the standard official-image
# dance: start as root, make the data dir writable, then drop to an unprivileged
# user before exec'ing the real command.
#
# Knobs (all optional):
#   PUID / PGID  UID/GID to run as and to own the data dir. Default 1000:1000.
#                Set these to match an EFS access point or a host user if needed.
#   DATA_DIR     Directory to ensure is writable. Default /app/data.
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    # Running as root (the usual case: local bind mount or default ECS task).
    # Point the runtime user at the requested UID/GID and hand it the data dir,
    # then drop privileges. This is the only moment we need root.
    if [ "$(id -u runner)" != "$PUID" ] || [ "$(id -g runner)" != "$PGID" ]; then
        groupmod -o -g "$PGID" runner 2>/dev/null || true
        usermod -o -u "$PUID" -g "$PGID" runner 2>/dev/null || true
    fi
    mkdir -p "$DATA_DIR"
    # Fix only the mount point's ownership (cheap). Files created by prior runs
    # are already owned by this UID, so no recursive chown on every startup.
    chown "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true
    exec gosu "$PUID:$PGID" "$@"
fi

# Already non-root (e.g. an ECS task definition pinned `user:`). We can't chown,
# so we trust the orchestrator provisioned a writable volume, and just run.
exec "$@"
