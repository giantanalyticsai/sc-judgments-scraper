"""Central configuration: everything portal-specific lives here.

If the SC portal changes an endpoint, payload field, or response shape, this is
the first (and usually only) file to touch.
"""

from pathlib import Path

# --- Portal endpoints --------------------------------------------------------
ROOT_URL = "https://scr.sci.gov.in"
SEARCH_URL = f"{ROOT_URL}/scrsearch/?p=pdf_search/home/"
CAPTCHA_IMAGE_URL = f"{ROOT_URL}/scrsearch/vendor/securimage/securimage_show.php"
CAPTCHA_CHECK_URL = f"{ROOT_URL}/scrsearch/?p=pdf_search/checkCaptcha"
PDF_OPEN_CAPTCHA_URL = f"{ROOT_URL}/scrsearch/?p=pdf_search/openpdfcaptcha"
PDF_OPEN_URL = f"{ROOT_URL}/scrsearch/?p=pdf_search/openpdf"
SESSION_INIT_URL = f"{ROOT_URL}/scrsearch/"

# --- Cookies -----------------------------------------------------------------
SESSION_COOKIE = "SCR_SESSID"
ALT_SESSION_COOKIE = "PHPSESSID"
ECOURTS_TOKEN_COOKIE = "JSESSION"

# --- Search behaviour --------------------------------------------------------
# fcourt_type=3 selects the Supreme Court. Dates in the payload use YYYY-MM-DD.
PAGE_SIZE = 1000
# The portal stops requiring a captcha per PDF for a while after a fresh
# session; after this many PDFs it starts demanding one again, so we re-init.
NO_CAPTCHA_BATCH_SIZE = 25
MAX_CAPTCHA_RETRIES = 10

# DataTables-style search payload. from_date/to_date/pagination are filled in
# per request by search.py. Captured from a real browser request.
DEFAULT_SEARCH_PAYLOAD = (
    "&sEcho=1&iColumns=2&sColumns=,&iDisplayStart=0&iDisplayLength=10"
    "&mDataProp_0=0&sSearch_0=&bRegex_0=false&bSearchable_0=true&bSortable_0=true"
    "&mDataProp_1=1&sSearch_1=&bRegex_1=false&bSearchable_1=true&bSortable_1=true"
    "&sSearch=&bRegex=false&iSortCol_0=0&sSortDir_0=asc&iSortingCols=1"
    "&search_txt1=&search_txt2=&search_txt3=&search_txt4=&search_txt5="
    "&pet_res=&state_code=&state_code_li=&dist_code=null&case_no=&case_year="
    "&from_date=&to_date=&judge_name=&reg_year=&fulltext_case_type=&act="
    "&judge_txt=&act_txt=&section_txt=&judge_val=&act_val=&year_val=&judge_arr="
    "&flag=&disp_nature=&search_opt=PHRASE&date_val=ALL&fcourt_type=3"
    "&citation_yr=&citation_vol=&citation_supl=&citation_page=&case_no1="
    "&case_year1=&pet_res1=&fulltext_case_type1=&citation_keyword=&sel_lang="
    "&proximity=&neu_cit_year=&neu_no=&ncn=&bool_opt=&sort_flg=&ajax_req=true&app_token="
)

# --- HTTP --------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 60

# --- Pacing & transient-error retries ----------------------------------------
# The portal throttles by volume, so we pace every request: sleep
# REQUEST_DELAY + uniform(0, REQUEST_DELAY_JITTER) seconds before each call.
REQUEST_DELAY = 1.0
REQUEST_DELAY_JITTER = 0.75
# Retries for transient failures (network drop, SSL, timeout, 5xx). Sleep grows
# as HTTP_BACKOFF_BASE * 2**attempt.
MAX_HTTP_RETRIES = 4
HTTP_BACKOFF_BASE = 2.0

# --- Throttle detection & ride-out -------------------------------------------
# The portal signals throttling silently: HTTP 200 with a 0-byte PDF body, and
# it throttles by VOLUME per source IP (~25 downloads), not by rate — so pacing
# (REQUEST_DELAY) alone cannot avoid it. When this many empty PDFs arrive
# back-to-back we treat it as throttling.
CONSECUTIVE_EMPTY_THRESHOLD = 5
# Rather than abort on a throttle, ride it out: the per-IP window resets within
# minutes, so cool down (escalating, capped), refresh the session, and resume.
# The escalation restarts from the base each time a record recovers, so steady-
# state is a flat ~THROTTLE_COOLDOWN_BASE wait between ~25-download installments.
THROTTLE_COOLDOWN_BASE = 120.0       # first cooldown, seconds
THROTTLE_COOLDOWN_MAX = 900.0        # cap a single cooldown at 15 min
# Give up (abort, exit 1) only after this many consecutive cooldowns fail to
# lift the throttle — by then it is almost certainly a hard block, not pacing.
MAX_THROTTLE_COOLDOWNS = 20

# Filename (under the output dir) where records that came back empty are logged
# so a later run can retry just those via --retry-failures.
FAILURES_FILENAME = "failures.json"
# After this many GENUINE failed retries (404 / no-file / errors — NOT throttle
# empties, which are ridden out above) a failure entry is marked "stale": it
# stays in failures.json for manual review but --retry-failures stops attempting
# it. Throttle empties never count toward this, so a throttled-but-downloadable
# judgment is never silently stranded.
STALE_RETRY_THRESHOLD = 5

# --- S3 backend ----------------------------------------------------------------
# Setting the S3_BUCKET env var switches storage to S3 (see storage.make_storage);
# S3_PREFIX overrides this default key prefix.
DEFAULT_S3_PREFIX = "sc-judgments/"

# --- Paths -------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = Path("./data")
CAPTCHA_DEBUG_DIR = Path("./captcha-debug")  # failed captchas saved here

# --- Captcha model -----------------------------------------------------------
# The ~95 MB model is not committed; it is fetched at runtime (and verified
# against the pinned hash) from its stable public home, then cached here.
CAPTCHA_MODEL_PATH = Path(__file__).parent / "captcha" / "captcha.onnx"
CAPTCHA_MODEL_URL = (
    "https://raw.githubusercontent.com/giantanalyticsai/"
    "district-court-judgements-scraper/main/src/captcha_solver/captcha.onnx"
)
CAPTCHA_MODEL_SHA256 = (
    "2a672587ee82eb010dbef54dd0a38e99625293608ed4068c4bd20ebe467fede4"
)
