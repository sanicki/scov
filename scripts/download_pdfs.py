"""Downloads a PDF for every flipbook in links.md that has no PDF yet.

Each flipbook is saved as <pdf_dir>/<title>.pdf, using its label in links.md.
Titles shared by several flipbooks get the doc name appended. <pdf_dir>/manifest.json
records which file belongs to which URL, so a PDF is renamed (not downloaded again)
when its title changes, and deleted when its link leaves links.md (for example
when a newer upload of the same issue replaces it). A flipbook is only saved when every page downloaded, so
a failed one is retried on the next run.

Usage: python scripts/download_pdfs.py [links.md] [PDF] [max_per_run]
"""

import io
import itertools
import json
import os
import re
import sys
import time

import img2pdf
from PIL import Image

from issuu_common import get, make_session, parse_issuu_url, read_links

# GitHub rejects files over 100 MB.
MAX_FILE_BYTES = 95 * 1024 * 1024
SKIPPED_FILE = "skipped.md"
MANIFEST_FILE = "manifest.json"


def pages_from_reader3(session, username, doc_name):
    res = get(
        session,
        f"https://reader3.isu.pub/{username}/{doc_name}/reader3_4.json",
        headers={"Referer": f"https://issuu.com/{username}/docs/{doc_name}", "Origin": "https://issuu.com"},
    )
    if res is None:
        return None
    try:
        pages = res.json()["document"]["pages"]
    except (ValueError, KeyError, TypeError):
        return None
    urls = []
    for page in pages:
        uri = page.get("imageUri") or page.get("imageUrl")
        if not uri:
            return None
        urls.append(uri if uri.startswith("http") else f"https://{uri}")
    return urls or None


def page_urls(document_id, page_count=None):
    """Page image URLs; open-ended (an iterator) when the page count is unknown."""
    if page_count is None:
        return (f"https://image.isu.pub/{document_id}/jpg/page_{n}.jpg" for n in itertools.count(1))
    return [
        f"https://image.isu.pub/{document_id}/jpg/page_{n}.jpg"
        for n in range(1, page_count + 1)
    ]


def pages_from_pub_api(session, username, doc_name):
    res = get(session, f"https://issuu.com/call/pub/v1/reader/pub/{username}/{doc_name}")
    if res is None:
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    page_count = data.get("pageCount")
    document_id = data.get("documentId")
    if not page_count or not document_id:
        return None
    return page_urls(document_id, int(page_count))


def unescape_page(text):
    """Undoes the escaping Issuu's pages apply to their embedded JSON data."""
    for _ in range(2):  # data can be escaped twice (JSON inside a JS string)
        text = text.replace("\\\\", "\\").replace('\\"', '"').replace("\\/", "/")
    return text.replace("&quot;", '"').replace("\\u002F", "/").replace("\\u0026", "&")


def find_document(text):
    """Returns (document id, page count or None) found in a flipbook page's HTML."""
    doc_id = re.search(r"image\.isu\.pub/([^/\"'\s?]+)/jpg/page_\d", text)
    if doc_id:
        doc_id = doc_id.group(1)
    else:
        rev = re.search(r'"revisionId"\s*:\s*"?(\w+)', text)
        pub = re.search(r'"publicationId"\s*:\s*"?(\w+)', text)
        doc = re.search(r'"documentId"\s*:\s*"([^"]+)"', text)
        doc_id = f"{rev.group(1)}-{pub.group(1)}" if rev and pub else (doc.group(1) if doc else None)
    count = re.search(r'"(?:pageCount|numberOfPages|pagesCount|totalPages)"\s*:\s*"?(\d+)', text)
    return doc_id, int(count.group(1)) if count else None


def pages_from_html(session, username, doc_name):
    res = get(session, f"https://issuu.com/{username}/docs/{doc_name}")
    if res is None:
        return None
    text = unescape_page(res.text)
    doc_id, count = find_document(text)
    print(f"  page HTML: document id {doc_id or 'not found'}, page count {count or 'not found'}")
    if not doc_id:
        # Log a few hints so the patterns above can be fixed if Issuu changes its pages.
        for m in list(re.finditer(r"isu\.pub|pageCount|publicationId|documentId", text))[:5]:
            print(f"    hint: …{text[max(0, m.start() - 80):m.end() + 80]!r}…")
        return None
    # Without a page count, download pages until one is missing.
    return page_urls(doc_id, count)


def resolve_pages(session, username, doc_name):
    for strategy in (pages_from_reader3, pages_from_pub_api, pages_from_html):
        urls = strategy(session, username, doc_name)
        if urls:
            count = len(urls) if isinstance(urls, list) else "unknown number of"
            print(f"  {count} pages via {strategy.__name__}")
            return urls
    raise RuntimeError("could not resolve page images")


def fetch_page(session, url):
    candidates = [url]
    if "image.isu.pub" in url:
        candidates.append(url.replace("image.isu.pub", "image.issuu.com"))
    for candidate in candidates:
        res = get(session, candidate, headers={"Referer": "https://issuu.com/"})
        if res is not None and res.content:
            return res.content
    return None


def to_jpeg(data, max_width=None, quality=None):
    """Re-encodes an image as JPEG (for non-JPEG pages or to shrink a PDF)."""
    with Image.open(io.BytesIO(data)) as img:
        if img.format == "JPEG" and max_width is None and quality is None:
            return data
        img = img.convert("RGB")
        if max_width and img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality or 90, optimize=True)
        return out.getvalue()


def build_pdf(pages):
    images = [to_jpeg(p) for p in pages]
    pdf = img2pdf.convert(images)
    if len(pdf) > MAX_FILE_BYTES:
        print(f"  PDF is {len(pdf) / 1e6:.0f} MB; recompressing pages...")
        pdf = img2pdf.convert([to_jpeg(p, max_width=1600, quality=60) for p in pages])
    return pdf


def download(session, url, out_path):
    username, doc_name = parse_issuu_url(url)
    urls = resolve_pages(session, username, doc_name)
    known = isinstance(urls, list)
    pages = []
    for n, page_url in enumerate(urls, 1):
        data = fetch_page(session, page_url)
        if data is None:
            if not known and pages:
                break  # past the last page
            total = len(urls) if known else "?"
            raise RuntimeError(f"page {n}/{total} failed: {page_url}")
        pages.append(data)
        time.sleep(0.2)
    pdf = build_pdf(pages)
    if len(pdf) > MAX_FILE_BYTES:
        return False, f"PDF is {len(pdf) / 1e6:.0f} MB, over GitHub's file size limit"
    tmp_path = out_path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(pdf)
    os.replace(tmp_path, out_path)
    return True, f"{len(pages)} pages, {len(pdf) / 1e6:.1f} MB"


def read_skipped(path):
    try:
        with open(path, encoding="utf-8") as f:
            return set(re.findall(r"https://issuu\.com/\S+?/docs/[^\s)]+", f.read()))
    except FileNotFoundError:
        return set()


def safe_filename(title, max_length=150):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_length].rstrip(" .")


def pdf_names(entries):
    """Maps each URL to a unique PDF file name derived from its title."""
    bases = {}
    for url, title in entries:
        doc_name = parse_issuu_url(url)[1]
        bases[url] = safe_filename(title or "") or doc_name
    counts = {}
    for base in bases.values():
        counts[base.lower()] = counts.get(base.lower(), 0) + 1
    names = {}
    for url, base in bases.items():
        doc_name = parse_issuu_url(url)[1]
        if counts[base.lower()] > 1 and base != doc_name:
            base = f"{base} ({doc_name})"
        names[url] = f"{base}.pdf"
    return names


def load_manifest(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_manifest(path, manifest):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(manifest.items())), f, indent=2, ensure_ascii=False)
        f.write("\n")


def prune(pdf_dir, manifest, names):
    """Deletes PDFs whose link left links.md (e.g. replaced by a newer upload)."""
    for url in [u for u in manifest if u not in names]:
        path = os.path.join(pdf_dir, manifest.pop(url))
        if os.path.exists(path):
            print(f"Removing {path} ({url} is no longer in links.md)")
            os.remove(path)


def sync_names(pdf_dir, manifest, names):
    """Renames already-downloaded PDFs whose title changed."""
    targets = {}
    for url, old in list(manifest.items()):
        new = names.get(url)
        if new is None or new == old:
            continue
        if not os.path.exists(os.path.join(pdf_dir, old)):
            del manifest[url]
            continue
        # Move via a temporary name so titles can swap between files.
        tmp = f".rename-{len(targets)}.tmp"
        os.replace(os.path.join(pdf_dir, old), os.path.join(pdf_dir, tmp))
        targets[url] = (tmp, new)
    for url, (tmp, new) in targets.items():
        print(f"Renaming {manifest[url]} -> {new}")
        os.replace(os.path.join(pdf_dir, tmp), os.path.join(pdf_dir, new))
        manifest[url] = new


def main():
    links_path = sys.argv[1] if len(sys.argv) > 1 else "links.md"
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "PDF"
    max_per_run = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    os.makedirs(pdf_dir, exist_ok=True)

    skipped_path = os.path.join(pdf_dir, SKIPPED_FILE)
    skipped = read_skipped(skipped_path)
    manifest_path = os.path.join(pdf_dir, MANIFEST_FILE)
    manifest = load_manifest(manifest_path)
    names = pdf_names(read_links(links_path))
    prune(pdf_dir, manifest, names)
    sync_names(pdf_dir, manifest, names)
    save_manifest(manifest_path, manifest)

    pending = []
    for url, name in names.items():
        out_path = os.path.join(pdf_dir, name)
        if url not in skipped and not (manifest.get(url) == name and os.path.exists(out_path)):
            pending.append((url, out_path))

    print(f"{len(pending)} flipbooks without a PDF; downloading up to {max_per_run}.")
    session = make_session()
    downloaded, newly_skipped, failed = 0, 0, []
    for url, out_path in pending[:max_per_run]:
        print(f"\n{url}")
        try:
            ok, detail = download(session, url, out_path)
        except Exception as e:
            ok, detail = None, str(e)
        print(f"  {'saved ' + out_path if ok else 'skipped' if ok is False else 'FAILED'}: {detail}")
        if ok:
            downloaded += 1
            manifest[url] = os.path.basename(out_path)
            save_manifest(manifest_path, manifest)
        elif ok is False:
            newly_skipped += 1
            with open(skipped_path, "a", encoding="utf-8") as f:
                f.write(f"- {url} — {detail}\n")
        else:
            failed.append(url)

    remaining = len(pending) - downloaded - newly_skipped
    print(f"\nDownloaded {downloaded}, failed {len(failed)}, {remaining} still without a PDF.")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"downloaded={downloaded}\nremaining={remaining}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"Downloaded {downloaded} PDF(s); {remaining} flipbook(s) still pending.\n")
            for url in failed:
                f.write(f"- Failed: {url}\n")
    return 1 if failed and not downloaded else 0


if __name__ == "__main__":
    sys.exit(main())
