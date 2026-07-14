#!/bin/sh
# Local daily scheduler for the SC judgments scraper.
#
# Waits for a wall-clock time (RUN_AT, "HH:MM") each day, then runs a scrape for
# that day. Designed to stay 100% local (no cron, no cloud) and to be easy to
# demonstrate: set RUN_AT a couple of minutes ahead, start the container, and
# watch it fire in the logs.
#
# Environment variables (see docker-compose.yml):
#   RUN_AT        Trigger time as "HH:MM", 24-hour, in the container's TZ. Default 23:30.
#   TZ            Timezone the clock (and RUN_AT) is interpreted in. Default UTC.
#                 Set e.g. Asia/Kolkata so RUN_AT matches the wall clock you watch.
#   OFFSET        Days before "today" to scrape (0 = today, 1 = yesterday). Default 0.
#   RUN_ON_START  If "true", run one scrape immediately at startup (demo escape
#                 hatch), then resume the daily schedule. Default false.
#   SCRAPE_ARGS   Extra args appended to `python scrape.py --daily` (e.g. "--delay 2").
set -eu

RUN_AT="${RUN_AT:-23:30}"
OFFSET="${OFFSET:-0}"
RUN_ON_START="${RUN_ON_START:-false}"
SCRAPE_ARGS="${SCRAPE_ARGS:-}"

run_scrape() {
    echo ">>> $(date '+%Y-%m-%d %H:%M:%S %Z') triggering daily scrape (offset=${OFFSET})"
    # shellcheck disable=SC2086
    python scrape.py --daily --offset "${OFFSET}" ${SCRAPE_ARGS} || \
        echo "!!! scrape exited non-zero (likely throttled) — see log above"
}

echo "Scheduler up. Daily run at ${RUN_AT} (TZ=${TZ:-UTC}), offset=${OFFSET} day(s)."

if [ "${RUN_ON_START}" = "true" ]; then
    echo ">>> RUN_ON_START set — running once now."
    run_scrape
fi

# Poll every 20s; fire when the current minute matches RUN_AT, then sleep past
# the minute so we don't double-fire within it.
while true; do
    if [ "$(date '+%H:%M')" = "${RUN_AT}" ]; then
        run_scrape
        sleep 61
    fi
    sleep 20
done
