"""Downloads a PDF for every flipbook in links.md that has no PDF yet.

Each flipbook is saved as <pdf_dir>/<doc_name>.pdf. A flipbook is only saved
when every page downloaded, so a failed one is retried on the next run.

Usage: python scripts/download_pdfs.py [links.md] [PDF] [max_per_run]
"""

import io
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


def pages_from_reader3(session, username, doc_name):
    res = get(session, f"https://reader3.isu.pub/{username}/{doc_name}/reader3_4.json")
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


def page_urls(document_id, page_count):
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


def pages_from_html(session, username, doc_name):
    res = get(session, f"https://issuu.com/{username}/docs/{doc_name}")
    if res is None:
        return None
    text = res.text.replace("\\/", "/")
    doc_id = re.search(r"image\.isu\.pub/([^/\"']+)/jpg/page_1", text) or re.search(
        r"\"documentId\"\s*:\s*\"([^\"]+)\"", text
    )
    count = re.search(r"\"pageCount\"\s*:\s*(\d+)", text)
    if not doc_id or not count:
        return None
    return page_urls(doc_id.group(1), int(count.group(1)))


def resolve_pages(session, username, doc_name):
    for strategy in (pages_from_reader3, pages_from_pub_api, pages_from_html):
        urls = strategy(session, username, doc_name)
        if urls:
            print(f"  {len(urls)} pages via {strategy.__name__}")
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
    pages = []
    for n, page_url in enumerate(urls, 1):
        data = fetch_page(session, page_url)
        if data is None:
            raise RuntimeError(f"page {n}/{len(urls)} failed: {page_url}")
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


def main():
    links_path = sys.argv[1] if len(sys.argv) > 1 else "links.md"
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "PDF"
    max_per_run = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    os.makedirs(pdf_dir, exist_ok=True)

    skipped_path = os.path.join(pdf_dir, SKIPPED_FILE)
    skipped = read_skipped(skipped_path)
    pending = []
    for url, _ in read_links(links_path):
        _, doc_name = parse_issuu_url(url)
        out_path = os.path.join(pdf_dir, f"{doc_name}.pdf")
        if url not in skipped and not os.path.exists(out_path):
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
