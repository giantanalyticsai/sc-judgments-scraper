"""Output layout and metadata persistence.

Files are laid out as:
    <output_dir>/<year>/<path>.pdf
    <output_dir>/<year>/<path>.json

`<year>` comes from the judgment path; records without a parseable year fall
back to an "unknown" bucket. Existing PDFs are treated as "already done", which
is what makes re-runs resumable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .models import DateRangeTask, JudgmentRecord

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)

    def _year_dir(self, record: JudgmentRecord) -> Path:
        year = str(record.year) if record.year is not None else "unknown"
        return self.output_dir / year

    def pdf_path(self, record: JudgmentRecord) -> Path:
        return self._year_dir(record) / f"{record.path}.pdf"

    def metadata_path(self, record: JudgmentRecord) -> Path:
        return self._year_dir(record) / f"{record.path}.json"

    def already_downloaded(self, record: JudgmentRecord) -> bool:
        pdf = self.pdf_path(record)
        return pdf.exists() and pdf.stat().st_size > 0

    def save(
        self, record: JudgmentRecord, pdf_bytes: bytes, task: DateRangeTask
    ) -> None:
        pdf_path = self.pdf_path(record)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pdf_bytes)

        metadata = {
            "path": record.path,
            "citation_year": record.citation_year,
            "nc_display": record.nc_display,
            "available_languages": record.language_codes,
            "search_from_date": task.from_date,
            "search_to_date": task.to_date,
            "scraped_at": datetime.now().isoformat(),
            "raw_html": record.raw_html,
        }
        self.metadata_path(record).write_text(json.dumps(metadata, indent=2))
        logger.debug("Saved %s (%d bytes)", pdf_path, len(pdf_bytes))
