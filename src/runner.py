"""Orchestration: tie session, search, download, and storage together.

Adds the throttle-survival behaviour learned from the portal:
  * downloads are paced + retried at the HTTP layer (see session.py);
  * a run of empty PDFs (the portal's silent throttle signal) triggers a
    bounded backoff and, if it persists, a clean abort with guidance;
  * records that come back empty are logged to failures.json for later retry.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config
from .downloader import Downloader
from .models import DateRangeTask, JudgmentRecord, generate_date_ranges
from .search import search_judgments
from .session import SCSession
from .storage import Storage

logger = logging.getLogger(__name__)


class ThrottleError(RuntimeError):
    """Raised when the portal appears to be throttling us (persistent empties)."""


@dataclass
class Stats:
    found: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    aborted: bool = False
    last_task: Optional[str] = None
    failures: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"found={self.found} downloaded={self.downloaded} "
            f"skipped={self.skipped} failed={self.failed}"
        )


def _failure_entry(record: JudgmentRecord, task: DateRangeTask, reason: str) -> dict:
    """Enough to reconstruct the record for a later --retry-failures run."""
    return {
        "path": record.path,
        "val": record.val,
        "citation_year": record.citation_year,
        "nc_display": record.nc_display,
        "language_codes": record.language_codes,
        "search_from_date": task.from_date,
        "search_to_date": task.to_date,
        "reason": reason,
        "failed_at": datetime.now().isoformat(),
    }


def _throttle_recovery(downloader: Downloader, record: JudgmentRecord, streak: int) -> Optional[bytes]:
    """Bounded exponential backoff to ride out a possibly-transient empty streak.

    Returns PDF bytes if a retry succeeds, else None (caller should abort).
    """
    for attempt in range(config.THROTTLE_RECOVERY_ATTEMPTS):
        wait = config.THROTTLE_BACKOFF_BASE * (2 ** attempt)
        logger.warning(
            "Suspected throttling (%d empty PDFs in a row). Backing off %.0fs, "
            "then retrying %s [%d/%d]",
            streak, wait, record.path, attempt + 1, config.THROTTLE_RECOVERY_ATTEMPTS,
        )
        time.sleep(wait)
        pdf_bytes = downloader.download(record)
        if pdf_bytes:
            logger.info("Recovered after backoff; continuing")
            return pdf_bytes
    return None


def run_scrape(
    start_date: str,
    end_date: str,
    day_step: int,
    output_dir: Path,
    delay: Optional[float] = None,
) -> Stats:
    """Scrape all judgments in [start_date, end_date] into output_dir."""
    session = SCSession(delay=delay)
    session.init()
    storage = Storage(output_dir)
    downloader = Downloader(session)
    stats = Stats()

    consecutive_empty = 0
    downloads_since_reset = 0

    try:
        for task in generate_date_ranges(start_date, end_date, day_step):
            stats.last_task = str(task)
            logger.info("=== Task %s ===", task)
            for record in search_judgments(session, task):
                stats.found += 1

                if storage.already_downloaded(record):
                    stats.skipped += 1
                    logger.debug("Skip (exists): %s", record.path)
                    continue

                # A fresh session resets the portal's per-session no-captcha budget.
                if downloads_since_reset >= config.NO_CAPTCHA_BATCH_SIZE:
                    logger.info("Refreshing session after %d downloads", downloads_since_reset)
                    session.init()
                    downloads_since_reset = 0

                try:
                    pdf_bytes = downloader.download(record)
                except Exception as exc:  # a single bad row shouldn't kill the run
                    logger.error("Error downloading %s: %s", record.path, exc)
                    stats.failed += 1
                    stats.failures.append(_failure_entry(record, task, f"error: {exc}"))
                    continue

                # Empty PDF: the portal's silent throttle signal. Once we've seen
                # a streak, try a bounded backoff before giving up on the record.
                if pdf_bytes is None and consecutive_empty + 1 >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                    pdf_bytes = _throttle_recovery(downloader, record, consecutive_empty + 1)

                if pdf_bytes:
                    storage.save(record, pdf_bytes, task)
                    stats.downloaded += 1
                    downloads_since_reset += 1
                    consecutive_empty = 0
                else:
                    stats.failed += 1
                    stats.failures.append(_failure_entry(record, task, "empty_pdf"))
                    consecutive_empty += 1
                    if consecutive_empty >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                        raise ThrottleError(consecutive_empty)

    except ThrottleError as exc:
        stats.aborted = True
        logger.error(
            "Aborting: %s empty PDFs in a row after backoff — the portal is "
            "almost certainly throttling this IP.",
            exc,
        )
    finally:
        _write_failures(output_dir, stats.failures)

    _log_outcome(stats, output_dir)
    return stats


def retry_failures(output_dir: Path, delay: Optional[float] = None) -> Stats:
    """Re-attempt only the records recorded in failures.json."""
    failures_path = Path(output_dir) / config.FAILURES_FILENAME
    if not failures_path.exists():
        logger.info("No failures file at %s; nothing to retry", failures_path)
        return Stats()

    entries = json.loads(failures_path.read_text())
    logger.info("Retrying %d failed record(s)", len(entries))

    session = SCSession(delay=delay)
    session.init()
    storage = Storage(output_dir)
    downloader = Downloader(session)
    stats = Stats()

    consecutive_empty = 0
    downloads_since_reset = 0
    still_failing: List[dict] = []

    try:
        for entry in entries:
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
                continue

            if downloads_since_reset >= config.NO_CAPTCHA_BATCH_SIZE:
                session.init()
                downloads_since_reset = 0

            pdf_bytes = downloader.download(record)
            if pdf_bytes is None and consecutive_empty + 1 >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                pdf_bytes = _throttle_recovery(downloader, record, consecutive_empty + 1)

            if pdf_bytes:
                storage.save(record, pdf_bytes, task)
                stats.downloaded += 1
                downloads_since_reset += 1
                consecutive_empty = 0
            else:
                stats.failed += 1
                still_failing.append(entry)
                consecutive_empty += 1
                if consecutive_empty >= config.CONSECUTIVE_EMPTY_THRESHOLD:
                    # Keep the records we haven't reached yet in the file too.
                    still_failing.extend(entries[entries.index(entry) + 1:])
                    raise ThrottleError(consecutive_empty)
    except ThrottleError as exc:
        stats.aborted = True
        logger.error("Aborting retry: %s empty PDFs in a row — still throttled.", exc)
    finally:
        _write_failures(output_dir, still_failing)

    _log_outcome(stats, output_dir)
    return stats


def _write_failures(output_dir: Path, failures: List[dict]) -> None:
    path = Path(output_dir) / config.FAILURES_FILENAME
    if failures:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(failures, indent=2))
        logger.info("Wrote %d failure record(s) to %s", len(failures), path)
    elif path.exists():
        # Nothing failed this run: clear a stale failures file.
        path.unlink()


def _log_outcome(stats: Stats, output_dir: Path) -> None:
    logger.info("Done: %s", stats.summary())
    if stats.aborted:
        logger.warning(
            "Run aborted at task %s. Saved PDFs are in %s. Wait for the throttle "
            "to lift (try again later or from another network), then re-run the "
            "same command to resume, or use --retry-failures to target the misses.",
            stats.last_task, output_dir,
        )
