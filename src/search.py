"""Date-range search with pagination.

Yields parsed JudgmentRecords for a DateRangeTask, walking the DataTables
pagination until the portal returns no more rows.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Iterator

from . import config
from .models import DateRangeTask, JudgmentRecord
from .parser import parse_row
from .session import SCSession

logger = logging.getLogger(__name__)


def _base_payload() -> dict:
    """Parse the canned query string into a mutable dict of single values."""
    parsed = urllib.parse.parse_qs(config.DEFAULT_SEARCH_PAYLOAD, keep_blank_values=True)
    payload = {k: v[0] for k, v in parsed.items()}
    payload["sEcho"] = 1
    payload["iDisplayStart"] = 0
    payload["iDisplayLength"] = config.PAGE_SIZE
    return payload


def _extract_rows(response: dict) -> list:
    report = response.get("reportrow", {})
    return report.get("aaData", []) if isinstance(report, dict) else []


def search_judgments(
    session: SCSession, task: DateRangeTask
) -> Iterator[JudgmentRecord]:
    """Yield every JudgmentRecord found for the given date range."""
    payload = _base_payload()
    payload["from_date"] = task.from_date
    payload["to_date"] = task.to_date

    page = 0
    while True:
        response = session.request(config.SEARCH_URL, payload)
        rows = _extract_rows(response)
        if not rows:
            if page == 0:
                logger.info("No judgments found for %s", task)
            break

        logger.info("Page %d: %d rows for %s", page + 1, len(rows), task)
        for row in rows:
            record = parse_row(row)
            if record is not None:
                yield record

        # Advance the DataTables cursor for the next page.
        page += 1
        payload["sEcho"] = int(payload["sEcho"]) + 1
        payload["iDisplayStart"] = int(payload["iDisplayStart"]) + config.PAGE_SIZE
