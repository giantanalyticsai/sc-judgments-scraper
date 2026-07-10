"""Fetch and cache the captcha ONNX model at runtime.

The model is ~95 MB, so it is intentionally NOT committed to the repo. Instead
it is downloaded on first use (and verified against a pinned hash) from its
stable public home in the district-court scraper repo.

This keeps the repo and any Docker image lean: the model becomes a cached
artifact rather than source. In a Docker build, pre-fetch it with:
    RUN uv run python fetch_model.py
so the image ships with the model already in place.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import requests

from ..config import CAPTCHA_MODEL_PATH, CAPTCHA_MODEL_SHA256, CAPTCHA_MODEL_URL

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(path: Path = CAPTCHA_MODEL_PATH) -> Path:
    """Return a local path to the captcha model, downloading it if missing.

    Re-downloads if an existing file fails the pinned-hash check (e.g. a
    truncated earlier download).
    """
    path = Path(path)
    if path.exists() and _sha256(path) == CAPTCHA_MODEL_SHA256:
        return path

    if path.exists():
        logger.warning("Captcha model at %s failed hash check; re-downloading", path)

    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading captcha model (~95 MB) from %s", CAPTCHA_MODEL_URL)
    tmp = path.with_suffix(path.suffix + ".part")
    with requests.get(CAPTCHA_MODEL_URL, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(_CHUNK):
                fh.write(chunk)

    actual = _sha256(tmp)
    if actual != CAPTCHA_MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Downloaded captcha model hash mismatch: expected "
            f"{CAPTCHA_MODEL_SHA256}, got {actual}"
        )
    tmp.replace(path)
    logger.info("Captcha model ready at %s", path)
    return path
