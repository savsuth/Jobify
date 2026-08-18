import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass
class NormalizedJob:
    source: str  # greenhouse | lever | web
    source_id: str
    company: str
    title: str
    location: str | None
    remote_type: str | None
    url: str
    description_raw: str
    employment_type: str  # full_time | part_time | internship | contract | temporary | unknown
    location_category: str  # us | non_us | location_unknown
    seniority: str  # entry_level | 0-1 | 1-2 | 2-3 | 3-5 | 5+ | unknown
    posted_at: datetime | None  # only set when the source actually provides one
    work_auth_status: str  # eligible | ineligible | unknown
    work_auth_reason: str | None  # why, when ineligible/unknown; None when eligible


def strip_html(raw: str) -> str:
    # Some sources (e.g. Greenhouse's `content` field) return HTML-escaped HTML
    # (`&lt;div&gt;` instead of `<div>`), so unescape before stripping tags.
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def guess_remote_type(location: str | None, title: str = "", description: str = "") -> str | None:
    """Text-based fallback for remote/hybrid/onsite. Prefer a source's structured
    signal when one exists (e.g. Lever's workplaceType) - only fall back to this."""
    haystack = " ".join(filter(None, [location, title, description])).lower()
    if "remote" in haystack:
        return "remote"
    if "hybrid" in haystack:
        return "hybrid"
    return "onsite" if location else None


# --- Location classification -------------------------------------------------
# Tiered by signal reliability: an authoritative country code (Lever) beats a
# structured office/region name (Greenhouse) beats free-text guessing. A job
# is only rejected as non_us when a marker is actually found - anything
# unrecognized stays location_unknown rather than being guessed either way.

_US_TEXT_PATTERNS = [
    r"\bunited states\b",
    r"\bu\.s\.a?\.?\b",
    r"\busa\b",
    r"\bus\b",
    r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
    r"\b(san francisco|new york|nyc|seattle|chicago|boston|austin|atlanta|los angeles|denver|miami|dallas|houston|philadelphia|washington|san diego|phoenix|portland|minneapolis|nashville|charlotte|detroit|columbus|pittsburgh|salt lake city|raleigh|san jose|sacramento)\b",
]

_NON_US_TEXT_PATTERNS = [
    r"\b(united kingdom|england|scotland|wales|ireland|canada|india|singapore|australia|mexico|france|germany|japan|china|taiwan|brazil|spain|netherlands|sweden|switzerland|poland|philippines|indonesia|colombia|turkey|israel|korea|vietnam|argentina|chile|belgium|denmark|norway|austria|italy|portugal|luxembourg|uae|united arab emirates|new zealand)\b",
    r"\b(london|toronto|dublin|mexico city|bengaluru|bangalore|mumbai|delhi|paris|barcelona|madrid|berlin|munich|amsterdam|tokyo|sydney|melbourne|seoul|hong kong|taipei|shanghai|beijing|sao paulo|buenos aires|montreal|vancouver|ottawa|edinburgh|manchester|warsaw|prague|vienna|zurich|stockholm|oslo|copenhagen|brussels|lisbon|milan|rome|tel aviv|dubai|jakarta|manila|bogota|santiago)\b",
]


def _text_location_category(text: str) -> str:
    t = (text or "").lower()
    has_us = any(re.search(p, t) for p in _US_TEXT_PATTERNS)
    has_non_us = any(re.search(p, t) for p in _NON_US_TEXT_PATTERNS)
    if has_us:
        # A listed US option (even among several offices) counts as reachable.
        return "us"
    if has_non_us:
        return "non_us"
    return "location_unknown"


def classify_location(
    location: str | None, country_code: str | None = None, office_names: list[str] | None = None
) -> str:
    """Returns "us", "non_us", or "location_unknown". Never guesses "us" for
    genuinely ambiguous input - only text with a recognized marker resolves."""
    if country_code:
        return "us" if country_code.strip().upper() == "US" else "non_us"
    if office_names:
        result = _text_location_category(" ".join(office_names))
        if result != "location_unknown":
            return result
    return _text_location_category(location or "")


# --- Employment type classification ------------------------------------------

_LEVER_COMMITMENT_MAP = {
    "full-time": "full_time",
    "full time": "full_time",
    "permanent": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "full time contractor": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "short term": "temporary",
}


def classify_employment_type(title: str, commitment: str | None = None) -> str:
    """commitment is a source-provided structured hint (e.g. Lever's
    categories.commitment) - trusted over text guessing when present. Greenhouse
    exposes no such field, so those jobs fall through to a conservative title-only
    heuristic and mostly end up "unknown" - that's an honest reflection of what
    the source actually tells us, not something to paper over."""
    if commitment:
        mapped = _LEVER_COMMITMENT_MAP.get(commitment.strip().lower())
        if mapped:
            return mapped

    t = title.lower()
    if re.search(r"\bintern(ship)?\b", t):
        return "internship"
    if re.search(r"\bcontract(or)?\b", t):
        return "contract"
    if re.search(r"\bpart[\s-]?time\b", t):
        return "part_time"
    if re.search(r"\btemporary\b|\btemp\b", t):
        return "temporary"
    return "unknown"


# --- Seniority classification -------------------------------------------------
# Checks are restricted to the TITLE, not the description body: a title
# stating "Senior"/"Staff"/"Principal"/"Lead" is an unambiguous, employer-
# declared signal, while the same words in free-text description prose
# (e.g. "collaborate with senior stakeholders") would false-positive. Never
# inspects the candidate's profile - seniority is a property of the posting.

_YEAR_PATTERN = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?", re.IGNORECASE)
_ENTRY_LEVEL_PATTERN = re.compile(
    r"\b(entry.level|entry level|new grad|recent graduate|early.career|early career)\b", re.IGNORECASE
)
_ENTRY_LEVEL_TITLE_PATTERN = re.compile(
    r"\b(entry.level|entry level|new grad|junior|jr\.?)\b", re.IGNORECASE
)
_SENIOR_TITLE_PATTERN = re.compile(r"\b(senior|sr\.?|staff|principal|lead)\b", re.IGNORECASE)


def classify_seniority(title: str, description: str) -> str:
    """Deterministic (regex-based), not an extra Claude call - keeps cost control
    intact. Title checked first (senior-tier, then entry-level-tier - see module
    note above), then falls back to the original description-based logic
    unchanged: an explicit "entry level"/"new grad" phrase, then "N years"
    language. Approximate by nature: covers the vast majority of real postings,
    not every phrasing."""
    if _SENIOR_TITLE_PATTERN.search(title or ""):
        return "senior"

    if _ENTRY_LEVEL_TITLE_PATTERN.search(title or "") or _ENTRY_LEVEL_PATTERN.search(description):
        return "entry_level"

    nums = [int(n) for n in _YEAR_PATTERN.findall(description) if 0 < int(n) <= 20]
    if not nums:
        return "unknown"

    min_years = min(nums)
    if min_years <= 1:
        return "0-1"
    if min_years <= 2:
        return "1-2"
    if min_years <= 3:
        return "2-3"
    if min_years <= 5:
        return "3-5"
    return "5+"


# --- Title relevance -----------------------------------------------------------
# Curated match against the candidate's target role families - replaces the
# old loose single-word matching that let ~52% clearly irrelevant roles
# (Customer Success Manager, Solutions Architect, ...) through to expensive
# ATS scoring. Exclusions win: a title must hit an include pattern AND no
# exclude pattern. Management/leadership titles are never treated as IC.

_EXCLUDE_PATTERNS = [
    r"\bmanager\b",
    r"\bmanagement\b",
    r"\bdirector\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\bhead of\b",
    r"\bchief\b",
    r"\baccount executive\b",
    r"\baccount manager\b",
    r"\bcustomer success\b",
    r"\bsales\b",
    r"\barchitect(ure)?\b",
    r"\bproduct (manager|marketing|lead|counsel)\b",
    r"\bsecurity engineer\b",
    r"\bmobile engineer\b",
    r"\bandroid engineer\b",
    r"\bios engineer\b",
    r"\breact native\b",
    r"\bfront[\s-]?end engineer\b",
    r"\bfirmware engineer\b",
    r"\bdesigner\b",
    r"\bdesign engineer\b",
    r"\blegal\b",
    r"\bcounsel\b",
    r"\bmarketing\b",
    r"\brecruiter\b",
    r"\brecruiting\b",
]

_INCLUDE_PATTERNS = [
    r"\bdata (platform |infrastructure )?engineer\b",
    r"\b(machine learning|ml) engineer\b",
    r"\bapplied ai engineer\b",
    r"\bai/ml platform engineer\b",
    r"\b(ai|ml) platform engineer\b",
    r"\bbackend\b.{0,20}\bengineer\b",  # covers "Backend Engineer", "Backend / API Engineer", "Backend Software Engineer"
    r"\bsoftware engineer\b",
    r"\bsde\s*(ii|2)\b",
    r"\bforward deployed (software )?engineer\b",
    r"\bsearch engineer\b",
    r"\bretrieval engineer\b",
    r"\bnlp engineer\b",
    r"\bllm engineer\b",
    r"\banalytics engineer\b",
    r"\bdata scientist\b",
    r"\bquantitative (developer|software engineer)\b",
    r"\bquant (developer|engineer)\b",
    r"\bsolutions engineer\b",
    r"\bcustomer engineer\b",
    r"\bai engineer\b",
    r"\binfrastructure engineer\b",
    r"\bplatform engineer\b",
]


def is_target_role(title: str) -> bool:
    """Whether a job title plausibly belongs to one of the candidate's actual
    target role families (see CLAUDE.md for the full list). Deliberately
    precise, not permissive - "Software Engineer II" and "SDE II" are meant to
    pass (seniority is judged separately, not by this filter); "Customer Success
    Manager", "Security Engineer", "Solutions Architect" etc. are meant to fail
    even though they share a word with a target title."""
    t = title.lower()
    if any(re.search(p, t) for p in _EXCLUDE_PATTERNS):
        return False
    return any(re.search(p, t) for p in _INCLUDE_PATTERNS)


# --- Work authorization (F-1/OPT) eligibility ----------------------------------
# Deterministic, not an extra Claude call. Deliberately conservative: only
# marks "ineligible" on unambiguous requirement language (avoids false
# positives from EEO non-discrimination boilerplate, which often mentions
# "citizenship status" in the OPPOSITE sense). Real ambiguity is "unknown"
# for manual review. Plain "authorized to work in the US" is explicitly NOT
# a trigger - OPT provides genuine work authorization.

# A stated alternative nearby (e.g. "...citizen, or authorized to work...")
# means OPT would satisfy the requirement - don't hard-reject, but flag it
# for a human glance.
_WORK_AUTH_ESCAPE_PATTERN = re.compile(
    r"\bor (?:[\w\s]{0,25})?(?:valid )?work (?:visa|authorization)\b"
    r"|\bor authorized to work\b"
    r"|\bor (?:[\w\s]{0,25})?visa holder\b",
    re.IGNORECASE,
)

_INELIGIBLE_PATTERNS = [
    (r"\bmust be an? (?:u\.?s\.?|united states) citizen\b", "Posting explicitly requires US citizenship"),
    (r"\b(?:u\.?s\.?|united states) citizenship (?:is )?required\b", "Posting explicitly requires US citizenship"),
    (r"\bu\.?s\.? citizens? only\b", "Posting explicitly requires US citizenship"),
    (
        r"\bmust be an? (?:u\.?s\.? )?permanent resident\b",
        "Posting explicitly requires permanent residency",
    ),
    (r"\bgreen card holders? only\b", "Posting explicitly requires a Green Card"),
    (
        r"\bmust (?:currently )?(?:hold|possess|have) an? (?:active )?(?:u\.?s\.? )?green card\b",
        "Posting explicitly requires a Green Card",
    ),
    (
        r"\bmust (?:be able to )?qualify as an? u\.?s\.? person\b",
        'Posting requires "US Person" status (export control)',
    ),
    (
        r"\bitar[\s-]?(?:restricted|compliant|controlled)\b|\bexport[\s-]control(?:led)?\b",
        "Posting indicates ITAR/export-control restrictions (typically requires US Person status)",
    ),
]

_CLEARANCE_CITIZEN_PATTERN = re.compile(
    r"citizen\w*[\s\S]{0,100}clearance|clearance[\s\S]{0,100}citizen\w*", re.IGNORECASE
)

_AMBIGUOUS_PATTERNS = [
    (
        r"\bsecurity clearance\b|\btop secret\b|\bts/sci\b|\bsecret clearance\b|\bclassified information\b",
        "Mentions a security clearance requirement without explicit citizenship language - review manually",
    ),
    (
        r"\bu\.?s\.? person\b",
        'Mentions "US Person" status - review manually for export-control implications',
    ),
    (
        r"\b(?:no|without|unable to provide) (?:visa )?sponsorship\b|\bsponsorship (?:is )?not (?:available|provided)\b",
        "Mentions visa sponsorship limitations - review manually for OPT/F-1 compatibility",
    ),
]


def classify_work_authorization(text: str) -> tuple[str, str | None]:
    """Returns (status, reason) where status is "eligible", "ineligible", or
    "unknown". `text` should be the job title + description combined."""
    t = text or ""

    if _CLEARANCE_CITIZEN_PATTERN.search(t):
        return "ineligible", "Posting ties a security clearance requirement explicitly to citizenship"

    for pattern, reason in _INELIGIBLE_PATTERNS:
        match = re.search(pattern, t, re.IGNORECASE)
        if match:
            window = t[match.end() : match.end() + 120]
            if _WORK_AUTH_ESCAPE_PATTERN.search(window):
                return (
                    "unknown",
                    "Mentions citizenship/permanent residency alongside a work-authorization "
                    "alternative - review manually",
                )
            return "ineligible", reason

    for pattern, reason in _AMBIGUOUS_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            return "unknown", reason

    return "eligible", None


# Cross-source deduplication. (source, source_id) alone isn't enough: web
# search uses the discovered URL as source_id, so the same still-open
# posting found via web search and later via the native connector gets two
# different (source, source_id) pairs.
#
# canonical_job_identity() is an additional identity signal on top of that,
# not a replacement (see discover_jobs() in pipeline.py, which checks both).
# Matches only on exact structural signals - a recognized ATS URL's company
# slug + native posting ID - never on title/company text similarity.

_LEVER_URL_PATTERN = re.compile(
    r"^https?://jobs\.lever\.co/([a-z0-9][a-z0-9-]*)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
# Lever's posting UUID is already globally unique, but we keep the company
# slug in the key too, mostly so it stays a readable/debuggable string.
_GREENHOUSE_URL_PATTERN = re.compile(
    r"^https?://(?:job-boards|boards)\.greenhouse\.io/([a-z0-9][a-z0-9-]*)/jobs/(\d+)",
    re.IGNORECASE,
)
# Some companies front Greenhouse with a custom URL carrying the job ID as
# a gh_jid query param instead of a /jobs/{id} path - e.g. every Stripe
# posting is ".../search?gh_jid=8114738", with no slug or per-job path.
# gh_jid is Greenhouse's own job-ID param - stripping it would collapse
# every Stripe posting onto one canonical key.
_GREENHOUSE_JID_PATTERN = re.compile(r"[?&]gh_jid=(\d+)")

# Tracking/referral params with no identity information - safe to strip
# before comparing URLs. gh_jid is deliberately not here (see above).
_TRACKING_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "lever-source", "lever-origin", "ref", "source", "src",
}


def canonical_job_identity(url: str) -> str:
    """Source-independent identity for a job posting URL. Three tiers,
    strongest first: a Lever/Greenhouse path URL (company slug + native ID
    from the path, not the NormalizedJob's own fields, which vary by
    source); a Greenhouse gh_jid query URL, keyed on the numeric ID alone;
    otherwise a normalized generic URL (lowercased, tracking params and
    fragment stripped) as the weakest, still-exact signal.

    Never returns None or raises - an unparseable url still produces a key
    via the generic fallback, so it's always safe as a dict key.
    """
    url = url or ""

    match = _LEVER_URL_PATTERN.match(url)
    if match:
        return f"lever:{match.group(1).lower()}:{match.group(2).lower()}"

    match = _GREENHOUSE_URL_PATTERN.match(url)
    if match:
        return f"greenhouse:{match.group(1).lower()}:{match.group(2)}"

    match = _GREENHOUSE_JID_PATTERN.search(url)
    if match:
        return f"greenhouse:jid:{match.group(1)}"

    parts = urlsplit(url)
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_QUERY_PARAMS
        )
    )
    path = parts.path.rstrip("/") or "/"
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))
    return f"url:{normalized}"


# Jobs get re-validated against the CURRENT filter rules, since the jobs
# table spans multiple generations of Discovery filter code and older rows
# were never retroactively re-checked. Mirrors _filter()'s four checks
# (non-US, non-full-time, off-target title, senior), but separates
# confirmed-bad from genuinely uncertain instead of a plain pass/reject.
#
# Only title, location text, description, and employment_type are on the
# Job row - office_names/country_code aren't persisted, so this re-derives
# from text only, which can be a *stronger* signal: Greenhouse's offices[]
# is company-wide, so a posting whose own text says "Toronto" can still get
# here after offices[] said "US" at ingest time.

_INELIGIBLE_EMPLOYMENT_TYPES = {"internship", "part_time", "contract", "temporary"}

CURRENTLY_ELIGIBLE = "CURRENTLY_ELIGIBLE"
CURRENTLY_INELIGIBLE = "CURRENTLY_INELIGIBLE"
AMBIGUOUS_REQUIRES_REVIEW = "AMBIGUOUS_REQUIRES_REVIEW"


def compute_current_eligibility(
    title: str, location: str | None, description: str, employment_type_stored: str | None = None
) -> tuple[str, list[str]]:
    """Re-checks one job against the current Discovery rules.

    Returns (status, reasons): status is CURRENTLY_ELIGIBLE,
    CURRENTLY_INELIGIBLE, or AMBIGUOUS_REQUIRES_REVIEW; reasons is empty
    when eligible.

    employment_type_stored is optional - without a source-provided
    commitment value, classify_employment_type(title) can only return
    "unknown" or an explicit non-full-time type, never a positive
    "full_time" (Greenhouse exposes no structured field at all). "unknown"
    stays non-blocking here too, matching _filter()'s behavior - treating
    it as blocking would mark nearly every Greenhouse job ambiguous.
    """
    loc = classify_location(location)
    sen = classify_seniority(title, description or "")
    role_ok = is_target_role(title)
    emp = employment_type_stored or classify_employment_type(title)

    ineligible_reasons = []
    if loc == "non_us":
        ineligible_reasons.append("non-US location")
    if sen == "senior":
        ineligible_reasons.append("senior/staff/principal/lead title")
    if not role_ok:
        ineligible_reasons.append("wrong role family")
    if emp in _INELIGIBLE_EMPLOYMENT_TYPES:
        ineligible_reasons.append(f"non-full-time employment ({emp})")

    if ineligible_reasons:
        if len(ineligible_reasons) > 1:
            return CURRENTLY_INELIGIBLE, ineligible_reasons + ["multiple violations"]
        return CURRENTLY_INELIGIBLE, ineligible_reasons

    if loc == "location_unknown":
        return AMBIGUOUS_REQUIRES_REVIEW, ["insufficient location evidence / ambiguous location"]

    return CURRENTLY_ELIGIBLE, []


# --- Posting-date freshness --------------------------------------------------
# Each source normalizer already fills in NormalizedJob.posted_at from the
# source's own original-publish signal (Greenhouse's first_published,
# Lever's createdAt, or on-page evidence from web search) - never
# discovered_at, never fabricated. Nothing filtered on it yet, though,
# which is what this adds.
#
# Freshness isn't stored as a column, unlike current_eligibility - "recent"
# changes meaning over time, so a stored snapshot would go stale. Only
# posted_at is persisted; freshness is computed live wherever needed. For
# the same reason, the gate lives in pipeline.py's persist-time layer, not
# aggregate.py's _filter(): an old or unknown-date job still needs to be
# persisted for history, it just shouldn't reach ATS.

POSTED_RECENTLY = "POSTED_RECENTLY"
POSTED_OLD = "POSTED_OLD"
POST_DATE_UNKNOWN = "POST_DATE_UNKNOWN"


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_freshness(posted_at: datetime | None, posted_within_days: int, now: datetime | None = None) -> str:
    """POSTED_RECENTLY / POSTED_OLD / POST_DATE_UNKNOWN, compared as UTC instants.

    posted_within_days must be a real int - the "gate disabled" case is
    handled by is_fresh_enough(), not here. A missing posted_at is always
    POST_DATE_UNKNOWN, never guessed. The window is a rolling
    posted_within_days*24h, inclusive at the boundary - a job posted exactly
    that long ago still counts as recent.
    """
    if posted_at is None:
        return POST_DATE_UNKNOWN
    now = _to_utc(now) if now is not None else datetime.now(timezone.utc)
    posted_at = _to_utc(posted_at)
    cutoff = now - timedelta(days=posted_within_days)
    return POSTED_RECENTLY if posted_at >= cutoff else POSTED_OLD


def is_fresh_enough(posted_at: datetime | None, posted_within_days: int | None, now: datetime | None = None) -> bool:
    """The Discovery gate. None disables it (nothing rejected on posting date);
    otherwise True only for POSTED_RECENTLY - an unknown date never passes."""
    if posted_within_days is None:
        return True
    return classify_freshness(posted_at, posted_within_days, now) == POSTED_RECENTLY


def days_since_posted(posted_at: datetime | None, now: datetime | None = None) -> int | None:
    """None when posted_at is unknown, never a fabricated value."""
    if posted_at is None:
        return None
    now = _to_utc(now) if now is not None else datetime.now(timezone.utc)
    return (now - _to_utc(posted_at)).days
