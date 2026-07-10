"""HTTP session + captcha authorization against the SC portal.

Owns the requests.Session (cookie jar), the initial handshake, captcha-based
session authorization, and a low-level request helper that:
  * paces every request (the portal throttles by volume), and
  * retries transient failures (network drop / SSL / timeout / 5xx) with
    exponential backoff.

Higher layers (search, downloader) go through `request()` / `get()` and treat
pacing, retries, captcha, and session mechanics as this module's concern.
"""

from __future__ import annotations

import logging
import random
import time
from io import BytesIO
from typing import Optional

import requests
import urllib3
from PIL import Image

from . import config
from .captcha import CaptchaSolver

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """Raised when the session cannot be established or authorized."""


class SCSession:
    def __init__(self, solver: Optional[CaptchaSolver] = None, delay: Optional[float] = None):
        self.solver = solver or CaptchaSolver()
        self.delay = config.REQUEST_DELAY if delay is None else delay
        self.http = requests.Session()
        self.http.verify = False
        self.http.headers.update(self._base_headers())

    @staticmethod
    def _base_headers() -> dict:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": config.ROOT_URL,
            "Pragma": "no-cache",
            "Referer": config.ROOT_URL + "/",
            "User-Agent": config.USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        }

    # --- low-level HTTP: pacing + transient retries --------------------------
    def _pace(self) -> None:
        time.sleep(self.delay + random.uniform(0, config.REQUEST_DELAY_JITTER))

    def _http(self, method: str, url: str, **kwargs) -> requests.Response:
        """Paced request with exponential-backoff retry on transient failures."""
        kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
        last_error = None
        for attempt in range(config.MAX_HTTP_RETRIES):
            self._pace()
            try:
                resp = self.http.request(method, url, **kwargs)
            except requests.exceptions.RequestException as exc:
                last_error = str(exc)
            else:
                if resp.status_code < 500:
                    return resp
                last_error = f"HTTP {resp.status_code}"

            if attempt < config.MAX_HTTP_RETRIES - 1:
                backoff = config.HTTP_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "Transient failure (%s) on %s [%d/%d]; retrying in %.0fs",
                    last_error, url, attempt + 1, config.MAX_HTTP_RETRIES, backoff,
                )
                time.sleep(backoff)
        raise SessionError(
            f"Request to {url} failed after {config.MAX_HTTP_RETRIES} attempts: {last_error}"
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        """Paced/retried GET for callers that need raw response bytes (PDFs)."""
        return self._http("GET", url, **kwargs)

    # --- lifecycle -----------------------------------------------------------
    def init(self) -> None:
        """Establish a fresh session and authorize it with a captcha."""
        self.http.cookies.clear()
        resp = self._http("GET", config.SESSION_INIT_URL)
        resp.raise_for_status()
        if not self.http.cookies.get(config.ECOURTS_TOKEN_COOKIE):
            raise SessionError(
                "No session token from portal (possible IP block / rate limit)."
            )
        self.authorize()

    def authorize(self, _depth: int = 0) -> None:
        """Solve a captcha and register it via checkCaptcha to unlock searching."""
        if _depth >= config.MAX_CAPTCHA_RETRIES:
            raise SessionError("Could not authorize session (captcha kept failing)")
        text = self._solve_captcha_text(config.CAPTCHA_IMAGE_URL)
        resp = self._http(
            "POST",
            config.CAPTCHA_CHECK_URL,
            data={"captcha": text, "search_opt": "PHRASE", "ajax_req": "true"},
        )
        if _safe_json(resp).get("captcha_status") != "Y":
            logger.debug("Captcha rejected during authorize, retrying")
            self.authorize(_depth + 1)

    # --- captcha helpers -----------------------------------------------------
    def _solve_captcha_text(self, image_url: str) -> str:
        """Fetch a captcha image and OCR it, retrying until it's 6 chars."""
        for _ in range(config.MAX_CAPTCHA_RETRIES):
            resp = self._http("GET", image_url)
            text = self.solver.solve(Image.open(BytesIO(resp.content)))
            if len(text) == 6:
                return text
            logger.debug("Captcha OCR gave %r (len %d), retrying", text, len(text))
        raise SessionError("Could not read a 6-char captcha after retries")

    def solve_image_captcha(self, image_url: str) -> str:
        """Public helper: OCR a captcha image at an absolute URL (PDF flow)."""
        return self._solve_captcha_text(image_url)

    # --- form request with session-expiry handling ---------------------------
    def request(self, url: str, payload: dict, _depth: int = 0) -> dict:
        """POST a form payload, returning parsed JSON.

        Re-authorizes and retries when the portal signals the session expired or
        returns an error message.
        """
        resp = self._http("POST", url, data=payload)
        data = _safe_json(resp)

        if _depth < 2 and (data.get("session_expire") == "Y" or "errormsg" in data):
            reason = data.get("errormsg", "session expired")
            logger.info("Re-authorizing session (%s)", reason)
            self.authorize()
            return self.request(url, payload, _depth + 1)

        return data


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
