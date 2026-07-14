# Supreme Court Judgments Scraper (local)

Date-range-driven scraper for Indian Supreme Court judgments from the SC digital
repository (`scr.sci.gov.in`). Give it a start and end date; it searches, solves
the portal captcha with a bundled ONNX model, paginates the results, and saves
the English PDF plus a metadata JSON for each judgment.

This is the "does it work end-to-end" phase. Failure-hardening (exponential
backoff, HTTP retry adapters, concurrency, a daily job) is intentionally out of
scope for now.

## Setup

```bash
uv sync
```

### Captcha model

The ~95 MB captcha model (`src/captcha/captcha.onnx`) is **not** committed. It is
downloaded and hash-verified on first use automatically. To pre-fetch it (e.g.
in a Docker build so the image ships with it cached):

```bash
uv run python fetch_model.py
```

## Docker (local)

Run everything in a container — no local Python/uv setup needed. The image bakes
the captcha model in at build time, and output is written to a mounted `./data`
directory on your host.

```bash
# Build once (the model is fetched during the build)
docker build -t sc-scraper .

# On-demand run — args after the image go straight to scrape.py
docker run --rm -v "$PWD/data:/app/data" \
  sc-scraper --start-date 2024-01-02 --end-date 2024-01-03
```

Or via Compose:

```bash
docker compose run --rm scraper --daily          # scrape today
docker compose run --rm scraper --retry-failures # retry prior misses
```

### Daily schedule (local, no cron/cloud)

The `scheduler` service runs a daily scrape at a wall-clock time you set. It's
built for demos: set the trigger a couple of minutes ahead and watch it fire.

```bash
docker compose up scheduler
```

Configure it in `docker-compose.yml` (or via env):

| Var | Default | Meaning |
|-----|---------|---------|
| `RUN_AT` | `23:30` | Daily trigger time, `HH:MM` (24h), in `TZ` |
| `TZ` | `Asia/Kolkata` | Timezone `RUN_AT` and "today" are interpreted in |
| `OFFSET` | `0` | Days before today to scrape (`1` = yesterday) |
| `RUN_ON_START` | `false` | Also run once immediately on startup (demo escape hatch) |
| `SCRAPE_ARGS` | — | Extra args appended to `scrape.py --daily` |

**Demo tip:** set `RUN_AT` to ~2 minutes ahead, `docker compose up scheduler`,
and follow the logs — you'll see the "triggering daily scrape" line fire on cue.

## Usage

```bash
uv run python scrape.py --start-date 2024-01-02 --end-date 2024-01-03
uv run python scrape.py --daily            # scrape today (local time)
uv run python scrape.py --daily --offset 1 # scrape yesterday
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--start-date` | (required*) | Inclusive start, `YYYY-MM-DD` |
| `--end-date`   | (required*) | Inclusive end, `YYYY-MM-DD` |
| `--day-step`   | `30`       | Days per search chunk |
| `--output-dir` | `./data`   | Where PDFs + metadata go |
| `--delay`      | `1.0`      | Base seconds between requests (pacing) |
| `--retry-failures` | off    | Retry only records in `<output-dir>/failures.json` |
| `--verbose`    | off        | Debug logging |

\* Dates are not required when `--retry-failures` is given.

## Throttling & resilience

The portal throttles by request volume and fails **silently** — it returns
HTTP 200 with a **0-byte PDF body** rather than an error. The scraper is built
around that:

- **Pacing**: every request waits `--delay` seconds (plus jitter) to stay under
  the limit.
- **Transient retries**: network drops, SSL errors, timeouts, and 5xx responses
  are retried with exponential backoff (`session.py`).
- **Throttle detection**: after `CONSECUTIVE_EMPTY_THRESHOLD` empty PDFs in a
  row, the scraper backs off; if empties persist it **aborts with guidance**
  rather than hammering the server.
- **Failed-record tracking**: records that fail are written to
  `<output-dir>/failures.json` with a specific `reason` (`empty_pdf`,
  `http_<status>`, `no_outputfile`, `error: ...`). After the throttle lifts,
  run `--retry-failures` to re-attempt just those without re-walking the range.
- **Retry accounting**: each failure entry carries `first_seen`, `retry_count`
  (incremented by `--retry-failures`), and `status`. After
  `STALE_RETRY_THRESHOLD` (default 5) failed retries an entry turns `stale`:
  it stays in `failures.json` for manual review but is no longer auto-retried.
- **Failure signals are distinguishable by exit code**: `0` success, `1`
  throttle abort (streak of empty PDFs), `2` usage error, `3` portal down
  (connection errors / persistent 5xx — nothing document-specific happened).

If a run aborts as throttled: wait for the limit to reset (try later, or from a
different network), then re-run the same command (already-downloaded PDFs are
skipped) or use `--retry-failures`.

## Output layout

```
data/
└── <year>/
    ├── <path>.pdf     # e.g. 2024_5_275_330.pdf
    └── <path>.json    # metadata (citation, languages, search range, raw row)
```

Re-running the same range skips judgments whose PDFs already exist, so runs are
resumable.

### S3 backend

Set `S3_BUCKET` (and optionally `S3_PREFIX`, default `sc-judgments/`) to write
the same layout — including `failures.json` — to S3 instead of local disk;
this is what makes scheduled runs on ECS Fargate durable across ephemeral
containers. Skip-existing checks then consult S3 (`HeadObject`). Credentials
come from boto3's default chain (task role on Fargate, `AWS_PROFILE`/SSO
locally) — never set static keys.

```sh
S3_BUCKET=sc-judgments-scraper-dev S3_PREFIX=judgments/ \
    uv run python scrape.py --daily --offset 1
```

## How it works

| Module | Responsibility |
|--------|----------------|
| `src/config.py`   | All portal endpoints, payloads, constants |
| `src/captcha/`    | ONNX captcha OCR (numpy-only inference); `model.py` fetches + verifies the model at runtime |
| `src/session.py`  | HTTP session, cookies, captcha authorization |
| `src/search.py`   | Paginated date-range search |
| `src/parser.py`   | Result-row HTML → `JudgmentRecord` |
| `src/downloader.py` | English PDF fetch (handles per-download captcha) |
| `src/storage.py`  | File layout, metadata, skip-existing |
| `src/runner.py`   | Orchestration + run stats |
| `scrape.py`       | CLI |

The modular split is deliberate: as data scales, a failure localizes to one
layer (captcha accuracy → `captcha/`, session expiry → `session.py`, portal HTML
drift → `parser.py`).

## Notes

- The captcha model (`src/captcha/captcha.onnx`) is a PARSeq recogniser reused
  from the district-court scraper; it solves the same securimage-style captcha.
- Dates are sent to the portal as `YYYY-MM-DD`. `fcourt_type=3` selects the
  Supreme Court.
