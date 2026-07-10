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

## Usage

```bash
uv run python scrape.py --start-date 2024-01-02 --end-date 2024-01-03
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
- **Failed-record tracking**: records that came back empty are written to
  `<output-dir>/failures.json`. After the throttle lifts, run
  `--retry-failures` to re-attempt just those without re-walking the range.

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
