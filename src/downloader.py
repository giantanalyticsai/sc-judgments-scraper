"""Fetch the English PDF for a judgment record.

The portal's PDF endpoint sometimes hands back the file link directly and
sometimes interposes a per-download captcha; both paths are handled here. Only
the English variant (lang_flg="") is fetched in this phase.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from bs4 import BeautifulSoup

from . import config
from .models import JudgmentRecord
from .session import SCSession

logger = logging.getLogger(__name__)

# A 315-byte body is the portal's "file not found" placeholder.
_EMPTY_PDF_SIZES = {0, 315}
_MAX_PDF_CAPTCHA_RETRIES = 5


class Downloader:
    def __init__(self, session: SCSession):
        self.session = session

    def _pdf_payload(self, record: JudgmentRecord) -> dict:
        return {
            "val": record.val,
            "path": record.path,
            "citation_year": record.citation_year,
            "nc_display": record.nc_display,
            "fcourt_type": "3",
            "ajax_req": "true",
            "lang_flg": "",  # English variant
        }

    def download(self, record: JudgmentRecord) -> Tuple[Optional[bytes], Optional[str]]:
        """Fetch the English PDF for a record.

        Returns (pdf_bytes, None) on success, or (None, reason) on failure.
        Reasons: "empty_pdf" (the portal's silent-throttle signal — the only
        one the caller counts toward the throttle streak), "http_<status>"
        (per-document HTTP error, e.g. 404), "no_outputfile" (portal gave no
        file link, e.g. captcha exhausted).
        """
        payload = self._pdf_payload(record)
        response = self.session.request(config.PDF_OPEN_CAPTCHA_URL, payload)

        # The portal may challenge with a captcha embedded in `filename` HTML.
        if _needs_pdf_captcha(response):
            response = self._solve_pdf_captcha(response, payload)

        output_file = response.get("outputfile") if response else None
        if not output_file:
            logger.error("No outputfile for %s: %s", record.path, response)
            return None, "no_outputfile"

        return self._fetch_file(output_file)

    def _solve_pdf_captcha(self, response: dict, payload: dict) -> dict:
        """Solve the embedded PDF captcha and re-request via openpdf."""
        img_src = _extract_captcha_img_src(response["filename"])
        if not img_src:
            logger.error("Could not locate captcha image in PDF response")
            return {}

        for attempt in range(_MAX_PDF_CAPTCHA_RETRIES):
            captcha_text = self.session.solve_image_captcha(config.ROOT_URL + img_src)
            payload["captcha1"] = captcha_text
            result = self.session.request(config.PDF_OPEN_URL, payload)
            if result.get("message") == "Invalid Captcha":
                logger.debug("PDF captcha rejected (attempt %d)", attempt + 1)
                continue
            return result
        logger.error("Exhausted PDF captcha retries")
        return {}

    def _fetch_file(self, output_file: str) -> Tuple[Optional[bytes], Optional[str]]:
        resp = self.session.get(
            config.ROOT_URL + output_file,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("HTTP %d fetching %s", resp.status_code, output_file)
            return None, f"http_{resp.status_code}"
        content = resp.content
        if len(content) in _EMPTY_PDF_SIZES:
            logger.warning("Empty/placeholder PDF (%d bytes)", len(content))
            return None, "empty_pdf"
        return content, None


def _needs_pdf_captcha(response: dict) -> bool:
    filename = response.get("filename", "") if response else ""
    return "securimage_show" in filename


def _extract_captcha_img_src(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img", {"id": "captcha_image_pdf"})
    if img and img.get("src"):
        return img["src"]
    # Fallback: any securimage image.
    img = soup.find("img", src=lambda s: s and "securimage_show" in s)
    return img["src"] if img else None
