"""Downloads a PDF for every flipbook in links.md that has no PDF yet.

Each flipbook is saved as <pdf_dir>/<title>.pdf, using its label in links.md.
Titles shared by several flipbooks get the doc name appended. <pdf_dir>/manifest.json
records which file belongs to which URL, so a PDF is renamed (not downloaded again)
when its title changes, and deleted when its link leaves links.md (for example
when a newer upload of the same issue replaces it). A flipbook is only saved when every page downloaded, so
a failed one is retried on the next run.

Each PDF also gets an OCR text layer (so it is searchable) and a Markdown copy
of its text in <text_dir>/<title>.md, for reading by people, search and LLMs.

Usage: python scripts/download_pdfs.py [links.md] [PDF] [max_per_run] [text]
"""

import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import img2pdf
import pikepdf
from PIL import Image

from issuu_common import get, make_session, parse_issuu_url, read_links
from naming import date_text, parse_date

# GitHub rejects files over 100 MB.
MAX_FILE_BYTES = 95 * 1024 * 1024
# Screen-readable, not print, quality: about a quarter of Issuu's page image size.
SCREEN_WIDTH = 1200
SCREEN_QUALITY = 60
PAGE_WIDTH_INCHES = 8.5  # so pages open at letter width at 100% zoom
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
    # The flipbook's own page works today; the APIs are kept as fallbacks.
    for strategy in (pages_from_html, pages_from_reader3, pages_from_pub_api):
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


def to_jpeg(data, max_width=SCREEN_WIDTH, quality=SCREEN_QUALITY):
    """Re-encodes a page image as a screen-sized JPEG."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
        dpi = img.width / PAGE_WIDTH_INCHES
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True, progressive=True, dpi=(dpi, dpi))
        return out.getvalue()


def build_pdf(pages):
    pdf = img2pdf.convert([to_jpeg(p) for p in pages])
    if len(pdf) > MAX_FILE_BYTES:
        print(f"  PDF is {len(pdf) / 1e6:.0f} MB; recompressing pages further...")
        pdf = img2pdf.convert([to_jpeg(p, max_width=1000, quality=50) for p in pages])
    return pdf


def pdf_page_images(path):
    """Returns the raw page images of a PDF built by build_pdf (one image per page)."""
    images = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            found = page.get_images() if hasattr(page, "get_images") else page.images
            for _, obj in found.items():
                images.append((int(obj.Width), bytes(obj.read_raw_bytes())))
    return images


def shrink_existing(pdf_dir, text_dir, manifest):
    """Re-encodes PDFs saved before the screen-size setting, in place."""
    for name in manifest.values():
        path = os.path.join(pdf_dir, name)
        if not os.path.exists(path):
            continue
        images = pdf_page_images(path)
        if not images or max(width for width, _ in images) <= SCREEN_WIDTH:
            continue
        before = os.path.getsize(path)
        pdf = build_pdf([data for _, data in images])
        with open(path + ".part", "wb") as f:
            f.write(pdf)
        os.replace(path + ".part", path)
        print(f"Shrank {name}: {before / 1e6:.1f} MB -> {len(pdf) / 1e6:.1f} MB")
        # Rebuilding drops the OCR text layer, so OCR this one again.
        if os.path.exists(text_path(text_dir, name)):
            os.remove(text_path(text_dir, name))


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


def prune(pdf_dir, text_dir, manifest, names):
    """Deletes PDFs whose link left links.md (e.g. replaced by a newer upload)."""
    for url in [u for u in manifest if u not in names]:
        name = manifest.pop(url)
        for path in (os.path.join(pdf_dir, name), text_path(text_dir, name)):
            if os.path.exists(path):
                print(f"Removing {path} ({url} is no longer in links.md)")
                os.remove(path)


def sync_names(pdf_dir, text_dir, manifest, names):
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
        # Renamed text is stale (its heading names the old title), so OCR runs again.
        if os.path.exists(text_path(text_dir, manifest[url])):
            os.remove(text_path(text_dir, manifest[url]))
        manifest[url] = new


def text_path(text_dir, pdf_name):
    return os.path.join(text_dir, pdf_name[:-4] + ".md")


def ocr_issue(pdf_path, md_path, url):
    """Adds an OCR text layer to pdf_path in place and writes its text to md_path."""
    with tempfile.TemporaryDirectory() as tmp:
        out_pdf, sidecar = os.path.join(tmp, "out.pdf"), os.path.join(tmp, "text.txt")
        subprocess.run(
            ["ocrmypdf", "--mode", "redo", "--output-type", "pdf", "--optimize", "0",
             "--jobs", str(os.cpu_count() or 1), "--language", "eng", "--quiet",
             "--sidecar", sidecar, pdf_path, out_pdf],
            check=True,
        )
        with open(sidecar, encoding="utf-8") as f:
            pages = f.read().split("\f")
        with pikepdf.open(out_pdf) as pdf:
            pages = (pages + [""] * len(pdf.pages))[: len(pdf.pages)]
        os.replace(out_pdf, pdf_path)
    name = os.path.basename(pdf_path)[:-4]
    year, month = parse_date(name)
    lines = [
        "---",
        f'title: "{name}"',
        f"date: {year}-{month:02d}" if year and month else (f"date: {year}" if year else "date: unknown"),
        f"source: {url}",
        f"pdf: PDF/{os.path.basename(pdf_path)}",
        f"pages: {len(pages)}",
        "text: Generated by OCR (Tesseract). Figures, names and table layouts may contain errors; check the PDF.",
        "---",
        "",
        f"# {name}",
        "",
        f"{date_text(year, month) or 'Date unknown'}. Original flipbook: {url}",
    ]
    for n, page in enumerate(pages, 1):
        text = re.sub(r"\n{3,}", "\n\n", page.strip())
        lines += ["", f"## Page {n}", "", text or "*(no text found on this page)*"]
    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(pages)


def main():
    links_path = sys.argv[1] if len(sys.argv) > 1 else "links.md"
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "PDF"
    max_per_run = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    text_dir = sys.argv[4] if len(sys.argv) > 4 else "text"
    os.makedirs(pdf_dir, exist_ok=True)

    skipped_path = os.path.join(pdf_dir, SKIPPED_FILE)
    skipped = read_skipped(skipped_path)
    manifest_path = os.path.join(pdf_dir, MANIFEST_FILE)
    manifest = load_manifest(manifest_path)
    names = pdf_names(read_links(links_path))
    prune(pdf_dir, text_dir, manifest, names)
    sync_names(pdf_dir, text_dir, manifest, names)
    shrink_existing(pdf_dir, text_dir, manifest)
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

    # OCR PDFs without text: this run's downloads first, then a batch of older ones.
    needs_text = [
        (url, name) for url, name in manifest.items()
        if os.path.exists(os.path.join(pdf_dir, name)) and not os.path.exists(text_path(text_dir, name))
    ]
    ocr_limit = downloaded + max_per_run
    print(f"\n{len(needs_text)} PDFs without text; OCRing up to {ocr_limit}.")
    ocr_done = 0
    for url, name in needs_text[:ocr_limit]:
        try:
            pages = ocr_issue(os.path.join(pdf_dir, name), text_path(text_dir, name), url)
            ocr_done += 1
            print(f"  OCR {name}: {pages} pages")
        except (subprocess.CalledProcessError, OSError) as e:
            failed.append(url)
            print(f"  OCR FAILED {name}: {e}")
    remaining += len(needs_text) - ocr_done
    progress = downloaded + ocr_done

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"progress={progress}\nremaining={remaining}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"Downloaded {downloaded} PDF(s) and OCRed {ocr_done}; {remaining} item(s) still pending.\n")
            for url in failed:
                f.write(f"- Failed: {url}\n")
    return 1 if failed and not progress else 0


if __name__ == "__main__":
    sys.exit(main())
