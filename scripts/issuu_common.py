"""Shared helpers for talking to Issuu and reading/writing links.md."""

import re
import time

import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LINK_RE = re.compile(r"https?://issuu\.com/([^/\s)]+)/docs/([^/?#\s)]+)")
LINE_RE = re.compile(r"^\s*[-*]\s*\[(?P<title>.*)\]\((?P<url>[^)\s]+)\)")


def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def get(session, url, retries=3, **kwargs):
    """GET with simple retry/backoff. Returns the response or None."""
    kwargs.setdefault("timeout", 60)
    for attempt in range(retries):
        try:
            res = session.get(url, **kwargs)
            if res.status_code == 200:
                return res
            if res.status_code in (404, 410):
                return None
            print(f"  [!] HTTP {res.status_code} for {url}")
        except requests.RequestException as e:
            print(f"  [!] {e.__class__.__name__} for {url}: {e}")
        time.sleep(2 ** (attempt + 1))
    return None


def doc_url(username, doc_name):
    return f"https://issuu.com/{username}/docs/{doc_name}"


def parse_issuu_url(url):
    match = LINK_RE.search(url)
    if not match:
        raise ValueError(f"Invalid Issuu URL: {url}")
    return match.group(1), match.group(2)


def read_links(path):
    """Returns an ordered list of (url, title) from links.md (missing file -> [])."""
    entries = []
    seen = set()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return entries
    for line in lines:
        match = LINK_RE.search(line)
        if not match:
            continue
        url = doc_url(match.group(1), match.group(2))
        if url in seen:
            continue
        seen.add(url)
        line_match = LINE_RE.match(line)
        title = line_match.group("title") if line_match else match.group(2)
        entries.append((url, title))
    return entries
