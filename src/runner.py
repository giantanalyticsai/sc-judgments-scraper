"""Orchestration: tie session, search, download, and storage together.

Adds the throttle-survival behaviour learned from the portal:
  * downloads are paced + retried at the HTTP layer (see session.py);
  * a run of empty PDFs (the portal's silent throttle signal) triggers a
    bounded backoff and, if it persists, a clean abort with guidance;
  * records that fail are logged to failures.json for later retry.

Three failure signals are kept distinguishable (they mean different things
operationally and map to different exit codes in scrape.py):
  * site down — connection errors / persistent 5xx while talking to the
    portal (SessionError). The whole run is moot: Stats.site_down, exit 3.
  * throttle — a streak of empty PDFs. Stats.aborted, exit 1 (existing).
  * per-document — one record came back empty/404/without a file link.
    Recorded in failures.json with a specific reason; the run continues.

failures.json entries carry retry accounting: `first_seen`, `retry_count`
(incremented by --retry-failures), and `status`. After
config.STALE_RETRY_THRESHOLD failed retries an entry turns "stale": it stays
in the file for manual review but is no longer auto-retried.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from . import config
from .downloader import Downloader
from .models import DateRangeTask, JudgmentRecord, generate_date_ranges
from .search import search_judgments
from .session import SCSession, SessionError
from .storage import Storage, make_storage

logger = logging.getLogger(__name__)


class ThrottleError(RuntimeError):
    """Raised when the portal appears to be throttling us (persistent empties)."""


@dataclass
class Stats:
    found: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    stale: int = 0
    aborted: bool = False
    site_down: bool = False
    last_task: Optional[str] = None
    failures: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"found={self.found} downloaded={self.downloaded} "
            f"skipped={self.skipped} failed={self.failed} stale={self.stale}"
        )


def _failure_entry(record: JudgmentRecord, task: DateRangeTask, reason: str) -> dict:
    """Enough to reconstruct the record for a later --retry-failures run."""
    now = datetime.now().isoformat()
    return {
        "path": record.path,
        "val": record.val,
        "citation_year": record.citation_year,
        "nc_display": record.nc_display,
        "language_codes": record.language_codes,
        "search_from_date": task.from_date,
        "search_to_date": task.to_date,
        "reason": reason,
        "failed_at": now,
        "first_seen": now,
        "retry_count": 0,
        "status": "active",
    }


def _normalize_entry(entry: dict) -> dict:
    """Backfill retry-accounting fields on entries written by older versions."""
    entry.setdefault("first_seen", entry.get("failed_at", datetime.now().isoformat()))
    entry.setdefault("retry_count", 0)
    entry.setdefault("status", "active")
    return entry


def _merge_failures(
    existing: List[dict], new_failures: List[dict], succeeded_paths: Set[str]
) -> List[dict]:
    """Fold this run's failures into the persisted file.

    Entries the run didn't touch (other dates, stale ones) survive unchanged;
    entries whose record was downloaded this run are dropped; a re-failure
    keeps its original first_seen/retry_count/status.
    """
    merged = {
        e["path"]: e for e in existing if e["path"] not in succeeded_paths
    }
    for entry in new_failures:
        prev = merged.get(entry["path"])
        if prev:
            entry["first_seen"] = prev["first_seen"]
            entry["retry_count"] = prev["retry_count"]
            entry["status"] = prev["status"]
        merged[entry["path"]] = entry
    return list(merged.values())


def _reconcile_retries(
    active: List[dict],
    attempted_failures: List[Tuple[dict, str]],
    succeeded_paths: Set[str],
    transient_run: bool,
) -> Tuple[List[dict], int]:
    """Compute the updated active-failure list after a --retry-failures run.

    Untouched entries (not attempted, e.g. beyond a throttle-abort point) pass
    through unchanged. For attempted-and-failed entries:
      * a genuine per-document failure (http_<status> / no_outputfile / error)
        always advances retry_count and may cross into `stale`;
      * an `empty_pdf` (the portal's throttle signal) advances retry_count ONLY
        when the run completed normally. If the run was throttle-aborted or
        portal-down (`transient_run`), the empties reflect a site-wide transient
        condition, not a bad document, so they are left unchanged — this is what
        stops sustained throttling from parking good docs as stale.

    Returns (updated_active_entries, newly_stale_count).
    """
    attempted_paths = {e["path"] for e, _ in attempted_failures} | succeeded_paths
    result = [e for e in active if e["path"] not in attempted_paths]
    newly_stale = 0
    for entry, reason in attempted_failures:
        if reason == "empty_pdf" and transient_run:
            result.append(entry)  # transient throttle/outage — do not penalise
            continue
        entry["retry_count"] += 1
        entry["failed_at"] = datetime.now().isoformat()
        entry["reason"] = reason
        if entry["retry_count"] >= config.STALE_RETRY_THRESHOLD:
            entry["status"] = "stale"
            newly_stale += 1
            logger.warning(
                "Marking %s stale after %d failed retries — manual review needed",
                entry["path"], entry["retry_count"],
            )
        result.append(entry)
    return result, newly_stale


def _throttle_recovery(
    downloader: Downloader, record: JudgmentRecord, streak: int
) -> Tuple[Optional[bytes], Optional[str]]:
    """Bounded exponential backoff to ride out a possibly-transient empty streak.

    Returns (pdf_bytes, None) if a retry succeeds, else (None, reason)
    (caller should abort).
    """
    reason: Optional[str] = "empty_pdf"
    for attempt in range(config.THROTTLE_RECOVERY_ATTEMPTS):
        wait = config.THROTTLE_BACKOFF_BASE * (2 ** attempt)
        logger.warning(
            "Suspected throttling (%d empty PDFs in a row). Backing off %.0fs, "
            "then retrying %s [%d/%d]",
            streak, wait, record.path, attempt + 1, config.THROTTLE_RECOVERY_ATTEMPTS,
        )
        time.sleep(wait)
        pdf_bytes, reason = downloader.download(record)
        if pdf_bytes:
            logger.info("Recovered after backoff; continuing")
            return pdf_bytes, None
    return None, reason


def run_scrape(
    start_date: str,
    end_date: str,
    day_step: int,
    output_dir: Path,
    delay: Optional[float] = None,
) -> Stats:
    """Scrape all judgments in [start_date, end_date] into the storage backend."""
    storage = make_storage(output_dir)
    logger.info("Storage: %s", storage)
    stats = Stats()

    session = SCSession(delay=delay)
    try:
        session.init()
    except SessionError as exc:
        stats.site_down = True
        logger.error("Portal unreachable, could not establish a session: %s", exc)
        return stats

    downloader = Downloader(session)
    consecutive_empty = 0
    downloads_since_reset = 0
    succeeded_paths: Set[str] = set()

    try:
        for task in generate_date_ranges(start_date, end_date, day_step):
            stats.last_task = str(task)
            logger.info("=== Task %s ===", task)
            for record in search_judgments(session, task):
                stats.found += 1

                if storage.already_downloaded(record):
                    stats.skipped += 1
                    succeeded_paths.add(record.path)
                    logger.debug("Skip (exists): %s", record.path)
                    continue

                # A fresh session resets the portal's per-session no-captcha budget.
                if downloads_since_reset >= config.NO_CAPTCHA_BATCH_SIZE:
                    logger.info("Refreshing session after %d downloads", downloads_since_reset)
                    session.init()
                    downloads_since_reset = 0

                try:
                    pdf_bytes, reason = downloader.download(record)
                except SessionError:
                    raise  # portal-level failure, not a per-record one
                except Exception as exc:  # a single bad row shouldn't kill the run
                    logger.error("Error downloading %s: %s", record.path, exc)
                    stats.failed += 1
                    stats.failures.append(_failure_entry(record, task, f"error: {exc}"))
                    continue

                # Empty PDF: the portal's silent throttle signal. Once we've seen
                # a streak, try a bounded backoff before giving up on the record.
                is_throttle_signal = reason == "empty_pdf"
                if (
                    is_throttle_signal
                    and consecutive_empty + 1 >= config.CONSECUTIVE_EMPTY_THRESHOLD
                ):
                    pdf_bytes, reason = _throttle_recovery(
                        downloader, record, consecutive_empty + 1
                    )
                    is_throttle_signal = reason == "empty_pdf"

                if pdf_bytes:
                    storage.save(record, pdf_bytes, task)
                    stats.downloaded += 1
                    downloads_since_reset += 1
                    succeeded_paths.add(record.path)
                    consecutive_empty = 0
                else:
                    stats.failed += 1
                    stats.failures.append(_failure_entry(record, task, reason or "unknown"))
                    if is_throttle_signal:
                        consecutive_empty += 1
                        if consecutive_empty >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                            raise ThrottleError(consecutive_empty)
                    else:
                        # A 404 etc. is this document's problem, not throttling.
                        consecutive_empty = 0

    except ThrottleError as exc:
        stats.aborted = True
        logger.error(
            "Aborting: %s empty PDFs in a row after backoff — the portal is "
            "almost certainly throttling this IP.",
            exc,
        )
    except SessionError as exc:
        stats.site_down = True
        logger.error("Portal unreachable mid-run (connection/5xx): %s", exc)
    finally:
        existing = [_normalize_entry(e) for e in storage.load_failures()]
        storage.save_failures(
            _merge_failures(existing, stats.failures, succeeded_paths)
        )

    _log_outcome(stats, storage)
    return stats


def retry_failures(output_dir: Path, delay: Optional[float] = None) -> Stats:
    """Re-attempt the active records recorded in failures.json.

    Each still-failing entry has its retry_count incremented; entries reaching
    config.STALE_RETRY_THRESHOLD are marked stale and skipped by future runs.
    """
    storage = make_storage(output_dir)
    logger.info("Storage: %s", storage)
    stats = Stats()

    entries = [_normalize_entry(e) for e in storage.load_failures()]
    if not entries:
        logger.info("No failures recorded; nothing to retry")
        return stats

    stale = [e for e in entries if e["status"] == "stale"]
    active = [e for e in entries if e["status"] != "stale"]
    stats.stale = len(stale)
    if stale:
        logger.warning(
            "%d stale record(s) (>=%d failed retries) need manual review: %s",
            len(stale), config.STALE_RETRY_THRESHOLD,
            ", ".join(e["path"] for e in stale),
        )
    if not active:
        logger.info("No active failures to retry")
        return stats
    logger.info("Retrying %d failed record(s)", len(active))

    session = SCSession(delay=delay)
    try:
        session.init()
    except SessionError as exc:
        stats.site_down = True
        logger.error("Portal unreachable, could not establish a session: %s", exc)
        return stats  # leave failures.json untouched

    downloader = Downloader(session)
    consecutive_empty = 0
    downloads_since_reset = 0
    succeeded_paths: Set[str] = set()
    # (entry, reason) per doc attempted-and-failed this run. Accounting is
    # deferred to the finally block (see _reconcile_retries) so empties from a
    # throttle-aborted / portal-down run don't advance the stale counter.
    attempted_failures: List[Tuple[dict, str]] = []

    try:
        for entry in active:
            record = JudgmentRecord(
                path=entry["path"],
                val=entry["val"],
                citation_year=entry["citation_year"],
                nc_display=entry["nc_display"],
                raw_html="",
                language_codes=entry.get("language_codes", [""]),
            )
            task = DateRangeTask(entry["search_from_date"], entry["search_to_date"])
            stats.found += 1

            if storage.already_downloaded(record):
                stats.skipped += 1
                succeeded_paths.add(record.path)
                continue

            if downloads_since_reset >= config.NO_CAPTCHA_BATCH_SIZE:
                session.init()
                downloads_since_reset = 0

            pdf_bytes, reason = downloader.download(record)
            is_throttle_signal = reason == "empty_pdf"
            if (
                is_throttle_signal
                and consecutive_empty + 1 >= config.CONSECUTIVE_EMPTY_THRESHOLD
            ):
                pdf_bytes, reason = _throttle_recovery(
                    downloader, record, consecutive_empty + 1
                )
                is_throttle_signal = reason == "empty_pdf"

            if pdf_bytes:
                storage.save(record, pdf_bytes, task)
                stats.downloaded += 1
                downloads_since_reset += 1
                succeeded_paths.add(record.path)
                consecutive_empty = 0
            else:
                stats.failed += 1
                attempted_failures.append((entry, reason or "unknown"))
                if is_throttle_signal:
                    consecutive_empty += 1
                    if consecutive_empty >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                        raise ThrottleError(consecutive_empty)
                else:
                    consecutive_empty = 0
    except ThrottleError as exc:
        stats.aborted = True
        logger.error("Aborting retry: %s empty PDFs in a row — still throttled.", exc)
    except SessionError as exc:
        stats.site_down = True
        logger.error("Portal unreachable mid-run (connection/5xx): %s", exc)
    finally:
        # Entries not attempted this run pass through unchanged; genuine
        # per-doc failures advance toward stale; empties from a throttle-aborted
        # or portal-down run are transient and are left unchanged.
        result, newly_stale = _reconcile_retries(
            active, attempted_failures, succeeded_paths,
            transient_run=stats.aborted or stats.site_down,
        )
        stats.stale += newly_stale
        storage.save_failures(stale + result)

    _log_outcome(stats, storage)
    return stats


def _log_outcome(stats: Stats, storage: Storage) -> None:
    logger.info("Done: %s", stats.summary())
    if stats.site_down:
        logger.warning(
            "Portal appears DOWN (connection errors / persistent 5xx). Nothing "
            "to do but wait; already-recorded failures are preserved for the "
            "next --retry-failures run."
        )
    elif stats.aborted:
        logger.warning(
            "Run aborted at task %s. Saved PDFs are in %s. Wait for the throttle "
            "to lift (try again later or from another network), then re-run the "
            "same command to resume, or use --retry-failures to target the misses.",
            stats.last_task, storage,
        )
