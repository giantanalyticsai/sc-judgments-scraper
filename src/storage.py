"""Output layout and metadata persistence (local disk or S3).

Files are laid out as:
    <output_dir>/<year>/<path>.pdf
    <output_dir>/<year>/<path>.json

`<year>` comes from the judgment path; records without a parseable year fall
back to an "unknown" bucket. Existing PDFs are treated as "already done", which
is what makes re-runs resumable.

Two backends share that layout:
  * `Storage` — local filesystem, the default for dev.
  * `S3Storage` — same keys under s3://<S3_BUCKET>/<S3_PREFIX>, used on ECS
    where the container disk is ephemeral. Selected by `make_storage()` when
    the S3_BUCKET env var is set. Credentials come from boto3's default chain
    (task role on Fargate, SSO profile locally) — never from explicit keys.

The failures file (`failures.json`) also lives behind the storage object so
`--retry-failures` works across ephemeral runs.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List

from . import config
from .models import DateRangeTask, JudgmentRecord

logger = logging.getLogger(__name__)


def make_storage(output_dir: Path) -> "Storage":
    """Pick the storage backend from the environment.

    S3_BUCKET set → S3 (S3_PREFIX optional); otherwise local `output_dir`.
    """
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        prefix = os.environ.get("S3_PREFIX", config.DEFAULT_S3_PREFIX)
        return S3Storage(bucket, prefix)
    return Storage(output_dir)


class Storage:
    """Local-filesystem backend."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)

    def __str__(self) -> str:
        return str(self.output_dir)

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
        self.metadata_path(record).write_text(
            json.dumps(_metadata(record, task), indent=2)
        )
        logger.debug("Saved %s (%d bytes)", pdf_path, len(pdf_bytes))

    # --- failures.json --------------------------------------------------------
    def _failures_path(self) -> Path:
        return self.output_dir / config.FAILURES_FILENAME

    def load_failures(self) -> List[dict]:
        path = self._failures_path()
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def save_failures(self, failures: List[dict]) -> None:
        path = self._failures_path()
        if failures:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(failures, indent=2))
            logger.info("Wrote %d failure record(s) to %s", len(failures), path)
        elif path.exists():
            # Nothing failed this run: clear a stale failures file.
            path.unlink()


class S3Storage(Storage):
    """S3 backend: same key layout as local, rooted at s3://bucket/prefix."""

    def __init__(self, bucket: str, prefix: str = ""):
        import boto3  # deferred so local runs don't pay the import

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.s3 = boto3.client("s3")
        self._not_found = self.s3.exceptions.ClientError

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _key(self, record: JudgmentRecord, ext: str) -> str:
        year = str(record.year) if record.year is not None else "unknown"
        return f"{self.prefix}{year}/{record.path}.{ext}"

    def already_downloaded(self, record: JudgmentRecord) -> bool:
        try:
            head = self.s3.head_object(
                Bucket=self.bucket, Key=self._key(record, "pdf")
            )
        except self._not_found as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return head["ContentLength"] > 0

    def save(
        self, record: JudgmentRecord, pdf_bytes: bytes, task: DateRangeTask
    ) -> None:
        pdf_key = self._key(record, "pdf")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=pdf_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(record, "json"),
            Body=json.dumps(_metadata(record, task), indent=2).encode(),
            ContentType="application/json",
        )
        logger.debug(
            "Saved s3://%s/%s (%d bytes)", self.bucket, pdf_key, len(pdf_bytes)
        )

    # --- failures.json --------------------------------------------------------
    def _failures_key(self) -> str:
        return f"{self.prefix}{config.FAILURES_FILENAME}"

    def load_failures(self) -> List[dict]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._failures_key())
        except self._not_found as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return []
            raise
        return json.loads(obj["Body"].read())

    def save_failures(self, failures: List[dict]) -> None:
        # Always overwrite (an empty list clears the file): the task role has
        # no s3:DeleteObject, by design.
        key = self._failures_key()
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(failures, indent=2).encode(),
            ContentType="application/json",
        )
        if failures:
            logger.info(
                "Wrote %d failure record(s) to s3://%s/%s",
                len(failures), self.bucket, key,
            )


def _metadata(record: JudgmentRecord, task: DateRangeTask) -> dict:
    return {
        "path": record.path,
        "citation_year": record.citation_year,
        "nc_display": record.nc_display,
        "available_languages": record.language_codes,
        "search_from_date": task.from_date,
        "search_to_date": task.to_date,
        "scraped_at": datetime.now().isoformat(),
        "raw_html": record.raw_html,
    }
