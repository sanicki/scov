"""Dates and consistent display names for flipbooks.

Names look like "Tipster – September 2026", "Divots – October 2019" or
"Tee to Green – March 2021". One-off publications keep their Issuu title with
its date normalised, e.g. "New Resident Booklet – August 2025". Repeated names
are numbered by upload order: the oldest keeps the plain name, later uploads
get " (2)", " (3)", ...

To fix a name by hand, add the flipbook's doc name (the last part of its Issuu
URL) to OVERRIDES.
"""

import re

from issuu_common import parse_issuu_url

OVERRIDES = {
    "scov_infobooklet": "SCOV Info Booklet",
}

MONTHS = [
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may", "may"), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sep"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
]
MONTH_NAMES = [full.capitalize() for full, _ in MONTHS]
# Full names (plus common misspellings) can appear glued to other text.
FULL_MONTH_RE = re.compile(
    r"(january|febuary|february|march|april|june|july|august|september|october|november|december)"
)
# Short forms must not touch other letters ("may" alone is too common a substring).
SHORT_MONTH_RE = re.compile(r"(?<![a-z])(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)(?![a-z])")
NOISE_RE = re.compile(r"tipster|divots|vistoso|issuu|teetogreen")
NUMERIC_DATE_RE = re.compile(r"(?<!\d)(19[89]\d|20\d\d)-(0[1-9]|1[0-2])(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")


def month_number(token):
    token = token.replace("febuary", "february")
    for n, (full, short) in enumerate(MONTHS, 1):
        if token == full or token.startswith(short):
            return n
    return None


def parse_date(text):
    """Returns (year, month) found in text; either may be None."""
    text = NOISE_RE.sub(" ", text.lower().replace("%20", " "))
    numeric = NUMERIC_DATE_RE.search(text)
    text = re.sub(r"[_\-.]+", " ", text)
    match = FULL_MONTH_RE.search(text) or SHORT_MONTH_RE.search(text)
    month = month_number(match.group(1)) if match else None
    if month is None and numeric:
        return int(numeric.group(1)), int(numeric.group(2))
    years = YEAR_RE.findall(text)
    year = int(years[0]) if years else None
    if year is None and match:
        # Two-digit year right after the month: "oct26", "july 18".
        short = re.match(r"\s*(\d{2})(?!\d)", text[match.end():])
        if short:
            year = 2000 + int(short.group(1))
    return year, month


def dated_entries(entries):
    """Adds (year, month, inferred) to each (url, title), filling gaps from newer neighbours."""
    result = []
    prev_year = prev_month = None
    for url, title in entries:
        doc_name = parse_issuu_url(url)[1]
        year, month = parse_date(title or "")
        doc_year, doc_month = parse_date(doc_name)
        year, month = year or doc_year, month or doc_month
        inferred = False
        if year is None and prev_year is not None:
            year, month, inferred = prev_year, month or prev_month, True
        elif month is None and year == prev_year:
            month, inferred = prev_month, True
        result.append((url, title, year, month, inferred))
        if year is not None:
            prev_year, prev_month = year, month
    return result


SERIES = (
    ("Tee to Green", re.compile(r"tee\W*(?:20)?to\W*(?:20)?green|teetogreen")),
    ("Divots", re.compile(r"divots")),
    ("Tipster", re.compile(r"tipster")),
)
# Words that mark a one-off issue of a series, which keeps its own title.
SPECIAL_RE = re.compile(r"anniversary|edition|special|handbook|booklet|guide", re.I)
DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?|(?:19|20)\d\d|"
    r"(?:jan|feb|febuary|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)\b",
    re.I,
)
SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def date_text(year, month):
    if year and month:
        return f"{MONTH_NAMES[month - 1]} {year}"
    return str(year) if year else ""


def base_name(title, doc_name, year, month, inferred):
    if doc_name in OVERRIDES:
        return OVERRIDES[doc_name]
    title = SUFFIX_RE.sub("", title or "")
    text = f"{title} {doc_name}".lower()
    if not SPECIAL_RE.search(text):
        for series, pattern in SERIES:
            if pattern.search(text):
                when = date_text(year, month)
                return f"{series} – {when}" if when else series
    # One-off publication: its own title without dates, plus a normalised date.
    name = re.sub(r"[_\s]+", " ", DATE_TOKEN_RE.sub(" ", title or doc_name))
    name = re.sub(r"\bissuu\b", " ", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -–,.")
    if name.islower():
        name = name.title()
    # Only trust a date written in the one-off's own title or doc name.
    own_year, own_month = parse_date(title or "")
    doc_year, doc_month = parse_date(doc_name)
    when = date_text(own_year or doc_year, own_month or doc_month)
    return f"{name} – {when}" if when else name


def display_names(entries):
    """Returns [(url, name)] for [(url, title)] in links.md order (newest first)."""
    bases = [
        base_name(title, parse_issuu_url(url)[1], year, month, inferred)
        for url, title, year, month, inferred in dated_entries(entries)
    ]
    # Number repeats from the oldest upload, so existing names never change.
    seen = {}
    names = [None] * len(bases)
    for i in reversed(range(len(bases))):
        seen[bases[i]] = seen.get(bases[i], 0) + 1
        names[i] = bases[i] if seen[bases[i]] == 1 else f"{bases[i]} ({seen[bases[i]]})"
    return [(url, name) for (url, _), name in zip(entries, names)]
