"""Data structures passed between the scraper's layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator, List, Optional


@dataclass
class DateRangeTask:
    """A [from_date, to_date] window (inclusive) to search, in YYYY-MM-DD."""

    from_date: str
    to_date: str

    def __str__(self) -> str:
        return f"{self.from_date}..{self.to_date}"


def generate_date_ranges(
    start_date: str, end_date: str, day_step: int
) -> Iterator[DateRangeTask]:
    """Chunk [start_date, end_date] inclusive into windows of `day_step` days."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=day_step - 1), end)
        yield DateRangeTask(
            current.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
        )
        current = window_end + timedelta(days=1)


@dataclass
class JudgmentRecord:
    """One judgment parsed from a search result row.

    `path` is the portal's opaque file identifier (e.g. "2024_5_275_330") and is
    what we key output files on. `val`, `citation_year`, `nc_display` are the
    fields the PDF-download endpoint requires.
    """

    path: str
    val: str
    citation_year: str
    nc_display: str
    raw_html: str
    language_codes: List[str] = field(default_factory=lambda: [""])

    @property
    def year(self) -> Optional[int]:
        """Judgment year, taken from the leading YYYY of the path if present."""
        head = self.path.split("_")[0]
        if len(head) == 4 and head.isdigit():
            return int(head)
        # Older format like S_1991_3_524_533
        parts = self.path.split("_")
        if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) == 4:
            return int(parts[1])
        return None
