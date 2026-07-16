"""Orchestration: tie session, search, download, and storage together.

Adds the throttle-survival behaviour learned from the portal:
  * downloads are paced + retried at the HTTP layer (see session.py);
  * a run of empty PDFs (the portal's silent, per-IP throttle signal) is ridden
    out — cool down, refresh the session, resume — rather than aborting, since
    the per-IP window resets within minutes. Only if the throttle never lifts
    after config.MAX_THROTTLE_COOLDOWNS does the run abort with guidance;
  * records that fail are logged to failures.json for later retry.

Three failure signals are kept distinguishable (they mean different things
operationally and map to different exit codes in scrape.py):
  * site down — connection errors / persistent 5xx while talking to the
    portal (SessionError). The whole run is moot: Stats.site_down, exit 3.
  * throttle — a streak of empty PDFs. Stats.aborted, exit 1 (existing).
  * per-document — one record came back empty/404/without a file link.
    Recorded in failures.json with a specific reason; the run continues.

failures.json entries carry retry accounting: `first_seen`, `retry_count`
(incremented by --retry-failures on GENUINE failures only, never on ridden-out
throttle empties), and `status`. After config.STALE_RETRY_THRESHOLD genuine
failed retries an entry turns "stale": it stays in the file for manual review
but is no longer auto-retried.
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


def _ride_out_throttle(
    session: SCSession, downloader: Downloader, record: JudgmentRecord, streak: int
) -> Tuple[Optional[bytes], Optional[str]]:
    """Ride out portal throttling instead of aborting.

    A sustained streak of empty PDFs means the portal is throttling this IP by
    volume. The per-IP window resets within minutes, so cool down (escalating up
    to THROTTLE_COOLDOWN_MAX), refresh the session, and re-attempt the record —
    up to config.MAX_THROTTLE_COOLDOWNS times. The session is refreshed on each
    cooldown, so the caller should reset its no-captcha download counter.

    Returns the first non-throttle outcome: (pdf_bytes, None) on success, or
    (None, reason) for a genuine per-document failure. If every cooldown still
    comes back empty, returns (None, "empty_pdf") and the caller aborts.
    Propagates SessionError if the portal is unreachable during a refresh.
    """
    pdf_bytes: Optional[bytes] = None
    reason: Optional[str] = "empty_pdf"
    for cooldown in range(config.MAX_THROTTLE_COOLDOWNS):
        wait = min(
            config.THROTTLE_COOLDOWN_BASE * (2 ** cooldown), config.THROTTLE_COOLDOWN_MAX
        )
        logger.warning(
            "Suspected throttling (%d empty PDFs in a row). Cooling down %.0fs, "
            "then refreshing the session and retrying %s [ride-out %d/%d]",
            streak, wait, record.path, cooldown + 1, config.MAX_THROTTLE_COOLDOWNS,
        )
        time.sleep(wait)
        session.init()  # fresh session/IP window; SessionError -> site_down
        pdf_bytes, reason = downloader.download(record)
        if pdf_bytes:
            logger.info("Throttle lifted after cooldown; resuming")
            return pdf_bytes, None
        if reason != "empty_pdf":
            # A genuine per-document failure (404 / no file), not throttling.
            return None, reason
    logger.error(
        "Throttle did not lift after %d cooldowns; treating as a hard block",
        config.MAX_THROTTLE_COOLDOWNS,
    )
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

                # Empty PDF: the portal's silent, per-IP throttle signal. Once
                # we've seen a streak, ride it out (cool down + refresh session +
                # retry) rather than giving up — the window resets in minutes.
                is_throttle_signal = reason == "empty_pdf"
                if (
                    is_throttle_signal
                    and consecutive_empty + 1 >= config.CONSECUTIVE_EMPTY_THRESHOLD
                ):
                    pdf_bytes, reason = _ride_out_throttle(
                        session, downloader, record, consecutive_empty + 1
                    )
                    downloads_since_reset = 0  # session was refreshed inside
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
            "Aborting: still %s empty PDFs in a row after %d throttle cooldowns — "
            "the portal is hard-blocking this IP, not just pacing.",
            exc, config.MAX_THROTTLE_COOLDOWNS,
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
    still_failing: List[dict] = []
    succeeded_paths: Set[str] = set()

    def _record_failure(entry: dict, reason: Optional[str]) -> None:
        entry["failed_at"] = datetime.now().isoformat()
        if reason:
            entry["reason"] = reason
        # Throttle empties are the portal's problem, not the document's: they are
        # ridden out above, and any that still land here must NOT count toward the
        # stale threshold — otherwise a throttled-but-downloadable judgment would
        # be stranded (marked stale and skipped forever). Only genuine per-record
        # failures (404 / no-file / errors) age an entry toward stale.
        if reason != "empty_pdf":
            entry["retry_count"] += 1
            if entry["retry_count"] >= config.STALE_RETRY_THRESHOLD:
                entry["status"] = "stale"
                stats.stale += 1
                logger.warning(
                    "Marking %s stale after %d failed retries — manual review needed",
                    entry["path"], entry["retry_count"],
                )
        still_failing.append(entry)

    try:
        for index, entry in enumerate(active):
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
                pdf_bytes, reason = _ride_out_throttle(
                    session, downloader, record, consecutive_empty + 1
                )
                downloads_since_reset = 0  # session was refreshed inside
                is_throttle_signal = reason == "empty_pdf"

            if pdf_bytes:
                storage.save(record, pdf_bytes, task)
                stats.downloaded += 1
                downloads_since_reset += 1
                succeeded_paths.add(record.path)
                consecutive_empty = 0
            else:
                stats.failed += 1
                _record_failure(entry, reason)
                if is_throttle_signal:
                    consecutive_empty += 1
                    if consecutive_empty >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                        # Keep the records we haven't reached yet in the file
                        # too, untouched (they weren't attempted).
                        still_failing.extend(active[index + 1:])
                        raise ThrottleError(consecutive_empty)
                else:
                    consecutive_empty = 0
    except ThrottleError as exc:
        stats.aborted = True
        logger.error(
            "Aborting retry: still %s empty PDFs after %d throttle cooldowns — "
            "portal hard-blocking this IP.",
            exc, config.MAX_THROTTLE_COOLDOWNS,
        )
    except SessionError as exc:
        stats.site_down = True
        logger.error("Portal unreachable mid-run (connection/5xx): %s", exc)
    finally:
        if stats.site_down:
            # A dead portal says nothing about individual records: put every
            # unresolved entry back unchanged (no retry_count increment).
            handled = {e["path"] for e in still_failing} | succeeded_paths
            still_failing.extend(e for e in active if e["path"] not in handled)
        storage.save_failures(stale + still_failing)

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
            "Run aborted at task %s: the throttle did not lift after %d cooldowns, "
            "so the portal is likely hard-blocking this IP. Saved PDFs are in %s. "
            "Try again later or from another network, then re-run the same command "
            "to resume, or use --retry-failures to target the misses.",
            stats.last_task, config.MAX_THROTTLE_COOLDOWNS, storage,
        )
