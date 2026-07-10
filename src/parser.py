"""Turn raw search-result rows into JudgmentRecord objects.

Isolated from search/download so that when the portal tweaks its result HTML,
only this module needs adjusting.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .models import JudgmentRecord

logger = logging.getLogger(__name__)

# onclick="javascript:open_pdf('3','2024','2024_5_275_330','2024INSC555')"
_OPEN_PDF_RE = re.compile(
    r"open_pdf\('(?P<val>.*?)','(?P<citation_year>.*?)','(?P<path>.*?)','(?P<nc_display>.*?)'\)"
)


def parse_row(row: list) -> Optional[JudgmentRecord]:
    """Parse one aaData row ([index, html_fragment]) into a JudgmentRecord.

    Returns None if the row has no usable PDF link (logged, not fatal).
    """
    html = row[1] if len(row) > 1 else ""
    soup = BeautifulSoup(html, "html.parser")

    button = soup.find("button", {"role": "link"})
    if not button or "onclick" not in button.attrs:
        logger.warning("Row has no PDF button; skipping")
        return None

    match = _OPEN_PDF_RE.search(button["onclick"])
    if not match:
        logger.warning("Could not parse open_pdf(...) from onclick; skipping")
        return None

    # A multi-language judgment exposes a <select name="language">; otherwise the
    # single (English) variant is represented by the empty language code.
    select = soup.find("select", {"name": "language"})
    if select:
        language_codes = [opt.get("value", "") for opt in select.find_all("option")]
    else:
        language_codes = [""]

    return JudgmentRecord(
        path=match.group("path").split("#")[0],
        val=match.group("val"),
        citation_year=match.group("citation_year"),
        nc_display=match.group("nc_display"),
        raw_html=html,
        language_codes=language_codes,
    )
