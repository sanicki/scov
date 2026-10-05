"""Collects every publication URL on an Issuu profile and merges them into links.md.

Links already in links.md are never removed, so a broken or partial fetch
cannot shrink the list. Flipbooks without a known title get one from their
own Issuu page; titles already in links.md are kept.

Usage: python scripts/fetch_links.py [username] [links.md]
"""

import html
import os
import re
import sys
import time

from issuu_common import doc_url, get, make_session, read_links


def fetch_reader_api(session, username, page_size=100):
    """Issuu's reader endpoint for a user's documents."""
    found = []
    offset = 0
    while True:
        res = get(
            session,
            f"https://issuu.com/call/pub/v1/reader/user/{username}/documents",
            params={"pageSize": page_size, "pageOffset": offset},
            headers={"Accept": "application/json"},
        )
        if res is None:
            break
        try:
            data = res.json()
        except ValueError:
            break
        results = data.get("results") or (data.get("data") or {}).get("documents") or []
        if not results:
            break
        for item in results:
            name = item.get("docName") or item.get("name") or item.get("uri")
            if name:
                found.append((name, item.get("title")))
        if len(results) < page_size:
            break
        offset += page_size
    return found


def fetch_profile_api(session, username, limit=100):
    """Issuu's profile endpoint, used by the profile page itself."""
    found = []
    offset = 0
    while True:
        res = get(
            session,
            f"https://issuu.com/call/profile/v1/documents/{username}",
            params={"offset": offset, "limit": limit},
            headers={"Accept": "application/json"},
        )
        if res is None:
            break
        try:
            data = res.json()
        except ValueError:
            break
        items = data.get("items") or data.get("documents") or data.get("results") or []
        if not items:
            break
        for item in items:
            name = item.get("uri") or item.get("docName") or item.get("name")
            if name:
                found.append((name.rsplit("/", 1)[-1], item.get("title")))
        if not data.get("hasMore", len(items) >= limit):
            break
        offset += len(items)
    return found


def fetch_profile_html(session, username, max_pages=50):
    """Scrapes /docs/ links out of the paginated profile HTML."""
    found = []
    seen = set()
    pattern = re.compile(
        rf"(?:issuu\.com)?\\?/{re.escape(username)}\\?/docs\\?/([A-Za-z0-9_.\-]+)"
    )
    for page in range(1, max_pages + 1):
        url = f"https://issuu.com/{username}" + (f"/{page}" if page > 1 else "")
        res = get(session, url, params={"ps": 24})
        if res is None:
            break
        new = [n for n in pattern.findall(res.text) if n not in seen]
        if not new:
            break
        for name in new:
            seen.add(name)
            found.append((name, None))
    return found


TITLE_RES = (
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', re.I),
    re.compile(r"<title[^>]*>([^<]+)</title>", re.I),
)
# Issuu page titles look like "Tipster October 2026 by SCV Communications - Issuu".
TITLE_SUFFIX_RE = re.compile(r"\s+(?:by\s+.+?\s+)?[-|–]\s*Issuu\s*$", re.I)


def fetch_title(session, url):
    """Returns the publication's display title from its page, or None."""
    res = get(session, url, retries=1)
    if res is None:
        return None
    for pattern in TITLE_RES:
        match = pattern.search(res.text)
        if match:
            title = TITLE_SUFFIX_RE.sub("", html.unescape(match.group(1))).strip()
            title = re.sub(r"\s+", " ", title)
            if title and title.lower() != "issuu":
                return title
    return None


def fill_titles(session, entries, max_failures=5):
    """Looks up titles for entries labelled only with their doc name.

    Stops after several consecutive failures so an Issuu outage can't stall the job.
    """
    filled, failures = [], 0
    for url, title in entries:
        doc_name = url.rsplit("/", 1)[-1]
        if (not title or title == doc_name) and failures < max_failures:
            found = fetch_title(session, url)
            failures = 0 if found else failures + 1
            title = found or title
            time.sleep(0.3)
        filled.append((url, title))
    looked_up = sum(1 for (_, a), (_, b) in zip(entries, filled) if a != b)
    print(f"Looked up {looked_up} new titles.")
    if failures >= max_failures:
        print(f"  [!] Stopped title lookups after {max_failures} consecutive failures.")
    return filled


def fetch_all(username):
    session = make_session()
    combined = {}
    for strategy in (fetch_reader_api, fetch_profile_api, fetch_profile_html):
        print(f"Trying {strategy.__name__}...")
        try:
            results = strategy(session, username)
        except Exception as e:  # keep going with the other strategies
            print(f"  [!] {strategy.__name__} failed: {e}")
            continue
        print(f"  [+] {len(results)} publications")
        for name, title in results:
            url = doc_url(username, name)
            if url not in combined or (title and not combined[url]):
                combined[url] = title
    return list(combined.items())


def write_links(path, username, entries):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {username} flipbooks\n\n")
        f.write(f"Publications from https://issuu.com/{username} ({len(entries)} total).\n")
        f.write("Updated daily by `.github/workflows/fetch-links.yml`.\n\n")
        for url, title in entries:
            label = (title or url.rsplit("/", 1)[-1]).replace("[", "(").replace("]", ")")
            f.write(f"- [{label}]({url})\n")


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "scvcommunications"
    links_path = sys.argv[2] if len(sys.argv) > 2 else "links.md"

    existing = read_links(links_path)
    fetched = fetch_all(username)
    print(f"Fetched {len(fetched)} unique publications; links.md has {len(existing)}.")

    if not fetched:
        print("Error: no publications found; leaving links.md untouched.")
        return 1

    # Issuu order (newest first) for everything it returns, then anything
    # previously recorded that Issuu no longer lists.
    existing_titles = dict(existing)
    fetched_urls = {url for url, _ in fetched}
    merged = [(url, title or existing_titles.get(url)) for url, title in fetched]
    merged += [(url, title) for url, title in existing if url not in fetched_urls]

    merged = fill_titles(make_session(), merged)

    new_count = len(fetched_urls - set(existing_titles))
    write_links(links_path, username, merged)
    print(f"Wrote {len(merged)} links to {links_path} ({new_count} new).")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new_count={new_count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
