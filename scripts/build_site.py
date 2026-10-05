"""Builds the GitHub Pages site listing every PDF in PDF/, newest first.

Dates come from each flipbook's title in links.md (see naming.py).

Usage: python scripts/build_site.py [links.md] [PDF] [_site]
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from issuu_common import read_links
from naming import MONTH_NAMES, dated_entries, parse_date

REPO = os.environ.get("GITHUB_REPOSITORY", "sanicki/scov")
BRANCH = os.environ.get("SITE_BRANCH", "main")

def load_pdfs(links_path, pdf_dir):
    """Returns the PDFs to list, newest first, as dicts."""
    try:
        with open(os.path.join(pdf_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        manifest = {}
    files = {n for n in os.listdir(pdf_dir) if n.lower().endswith(".pdf")} if os.path.isdir(pdf_dir) else set()

    items = []
    listed = set()
    for order, (url, title, year, month, inferred) in enumerate(dated_entries(read_links(links_path))):
        name = manifest.get(url)
        if name in files:
            listed.add(name)
            items.append(dict(name=name, title=title or name[:-4], year=year, month=month,
                              inferred=inferred, order=order, source=url))
    # PDFs added to the folder by hand.
    for order, name in enumerate(sorted(files - listed), len(items) + 10_000):
        year, month = parse_date(name[:-4])
        items.append(dict(name=name, title=name[:-4], year=year, month=month,
                          inferred=False, order=order, source=None))

    items.sort(key=lambda i: (-(i["year"] or 0), -(i["month"] or 0), i["order"]))
    return items


def pdf_url(kind, name):
    return f"https://github.com/{REPO}/{kind}/{BRANCH}/PDF/{quote(name)}"


def date_label(item):
    if not item["year"]:
        return "Date unknown"
    label = f"{MONTH_NAMES[item['month'] - 1]} {item['year']}" if item["month"] else str(item["year"])
    return f"{label} (estimated)" if item["inferred"] else label


ICON_PDF = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 2H8c-1.1 0-2 '
    ".9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-8.5 7.5c0 .83-.67 1.5-1.5 1.5H9v2H7.5V7H10c.83 "
    "0 1.5.67 1.5 1.5v1zm5 2c0 .83-.67 1.5-1.5 1.5h-2.5V7H15c.83 0 1.5.67 1.5 1.5v3zm4-3H19v1h1.5V11H19v2h-1.5V7h3v1.5"
    "zM9 9.5h1v-1H9v1zM4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm10 5.5h1v-3h-1v3z\"/></svg>"
)
ICON_DOWNLOAD = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z"/></svg>'
)
ICON_SEARCH = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M15.5 14h-.79l-.28-.27A6.47 '
    "6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 "
    '14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>'
)


def render(items, generated):
    esc = html.escape
    years = []
    for item in items:
        key = str(item["year"]) if item["year"] else "undated"
        if not years or years[-1][0] != key:
            years.append((key, []))
        years[-1][1].append(item)

    nav = "\n".join(
        f'<li><a class="chip" href="#y-{key}">{"Undated" if key == "undated" else key}</a></li>'
        for key, _ in years
    )
    sections = []
    for key, group in years:
        heading = "Undated" if key == "undated" else key
        rows = []
        for item in group:
            title, label = esc(item["title"]), esc(date_label(item))
            # Most names already end in their date; only show it when they don't.
            supporting = "" if date_label(item) in item["title"] else f'<span class="supporting">{label}</span>'
            search = esc(f"{item['title']} {date_label(item)}".lower(), quote=True)
            rows.append(f"""<li class="item" data-search="{search}">
  <span class="leading">{ICON_PDF}</span>
  <div class="text">
    <a class="headline" href="{esc(pdf_url('blob', item['name']))}">{title}</a>
    {supporting}
  </div>
  <a class="icon-button" href="{esc(pdf_url('raw', item['name']))}" download>{ICON_DOWNLOAD}<span class="visually-hidden">Download {title}</span></a>
</li>""")
        sections.append(f"""<section class="year" aria-labelledby="y-{key}-h" id="y-{key}">
  <h2 id="y-{key}-h">{heading} <span class="count">· {len(group)}</span></h2>
  <ul class="list card" role="list">
{chr(10).join(rows)}
  </ul>
</section>""")

    body = "\n".join(sections) if sections else '<p class="empty">No PDFs have been archived yet.</p>'
    return TEMPLATE.format(
        count=len(items),
        plural="" if len(items) == 1 else "s",
        nav=nav,
        sections=body,
        generated=generated.strftime("%B %-d, %Y"),
        generated_iso=generated.isoformat(timespec="seconds"),
        repo=esc(REPO),
        icon_search=ICON_SEARCH,
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCV Flipbook Archive</title>
<meta name="description" content="PDF archive of the SCV Communications flipbooks, newest first.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&display=swap">
<style>
/* Material Design 3 color roles, generated from a teal seed. */
:root {{
  color-scheme: light dark;
  --primary: #006a6a; --on-primary: #ffffff;
  --primary-container: #9cf1f0; --on-primary-container: #002020;
  --secondary-container: #cce8e7; --on-secondary-container: #051f1f;
  --surface: #f4fbfa; --on-surface: #161d1d; --on-surface-variant: #3f4948;
  --surface-container-low: #eff5f4; --surface-container: #e9efee;
  --surface-container-high: #e3e9e8; --outline: #6f7979; --outline-variant: #bec9c8;
  --shadow: rgb(0 0 0 / 0.15);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --primary: #80d5d4; --on-primary: #003737;
    --primary-container: #004f4f; --on-primary-container: #9cf1f0;
    --secondary-container: #324b4b; --on-secondary-container: #cce8e7;
    --surface: #0e1514; --on-surface: #dde4e3; --on-surface-variant: #bec9c8;
    --surface-container-low: #161d1d; --surface-container: #1a2121;
    --surface-container-high: #252b2b; --outline: #889392; --outline-variant: #3f4948;
    --shadow: rgb(0 0 0 / 0.4);
  }}
}}
*, *::before, *::after {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
html {{ scroll-behavior: smooth; scroll-padding-top: 80px; }}
@media (prefers-reduced-motion: reduce) {{
  html {{ scroll-behavior: auto; }}
  * {{ transition: none !important; }}
}}
body {{
  margin: 0; background: var(--surface); color: var(--on-surface);
  font: 400 1rem/1.5 Roboto, system-ui, -apple-system, "Segoe UI", sans-serif;
}}
a {{ color: var(--primary); }}
:focus-visible {{ outline: 3px solid var(--primary); outline-offset: 2px; border-radius: 4px; }}
.visually-hidden {{
  position: absolute !important; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}}
.skip-link {{
  position: absolute; left: 16px; top: -100px; z-index: 10; padding: 12px 16px;
  background: var(--primary); color: var(--on-primary); border-radius: 8px; font-weight: 500;
}}
.skip-link:focus {{ top: 16px; }}
.icon {{ width: 24px; height: 24px; fill: currentColor; flex: none; }}

/* Top app bar */
.top-app-bar {{
  position: sticky; top: 0; z-index: 5; background: var(--surface-container);
  box-shadow: 0 1px 3px var(--shadow);
}}
.top-app-bar .inner {{ max-width: 960px; margin: 0 auto; padding: 12px 16px; }}
.top-app-bar h1 {{ margin: 0; font-size: 1.375rem; line-height: 1.75rem; font-weight: 400; }}
.top-app-bar p {{ margin: 0; color: var(--on-surface-variant); font-size: .875rem; }}

main {{ max-width: 960px; margin: 0 auto; padding: 16px 16px 48px; }}

/* Outlined text field */
.search {{ position: relative; margin: 8px 0 16px; }}
.search label {{ display: block; margin-bottom: 4px; font-size: .875rem; font-weight: 500; color: var(--on-surface-variant); }}
.search .field {{ position: relative; }}
.search .icon {{ position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--on-surface-variant); }}
.search input {{
  width: 100%; min-height: 56px; padding: 0 16px 0 48px; font: inherit; color: var(--on-surface);
  background: transparent; border: 1px solid var(--outline); border-radius: 4px;
}}
.search input:hover {{ border-color: var(--on-surface); }}
.search input:focus {{ outline: none; border: 2px solid var(--primary); padding-left: 47px; }}
#status {{ min-height: 1.5em; margin: 0 0 8px; color: var(--on-surface-variant); font-size: .875rem; }}

/* Assist chips for jumping to a year */
.year-nav ul {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 24px; padding: 0; list-style: none; }}
.chip {{
  display: inline-flex; align-items: center; min-height: 32px; padding: 6px 16px;
  border: 1px solid var(--outline); border-radius: 8px; color: var(--on-surface-variant);
  text-decoration: none; font-size: .875rem; font-weight: 500; position: relative;
}}
.chip::after {{ content: ""; position: absolute; inset: -8px 0; }} /* 48px touch target */
.chip:hover {{ background: var(--secondary-container); color: var(--on-secondary-container); }}

/* Lists in filled cards */
.year h2 {{ margin: 24px 0 8px; font-size: 1.375rem; font-weight: 400; }}
.year h2 .count {{ color: var(--on-surface-variant); font-size: 1rem; }}
.card {{ background: var(--surface-container-low); border-radius: 12px; box-shadow: 0 1px 2px var(--shadow); }}
.list {{ margin: 0; padding: 8px 0; list-style: none; }}
.item {{ display: flex; align-items: center; gap: 16px; min-height: 72px; padding: 8px 8px 8px 16px; position: relative; }}
.item + .item {{ border-top: 1px solid var(--outline-variant); }}
.item:hover {{ background: var(--surface-container-high); }}
.leading {{
  display: grid; place-items: center; width: 40px; height: 40px; flex: none; border-radius: 50%;
  background: var(--primary-container); color: var(--on-primary-container);
}}
.text {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
.headline {{ color: var(--on-surface); text-decoration: none; font-weight: 500; overflow-wrap: anywhere; }}
.headline:hover {{ text-decoration: underline; }}
/* Make the whole row open the PDF, keeping the download button on top. */
.headline::after {{ content: ""; position: absolute; inset: 0; }}
.headline:focus-visible {{ outline: none; }}
.headline:focus-visible::after {{ outline: 3px solid var(--primary); outline-offset: -3px; border-radius: 12px; }}
.supporting {{ color: var(--on-surface-variant); font-size: .875rem; }}
.icon-button {{
  position: relative; z-index: 1; display: grid; place-items: center; width: 48px; height: 48px;
  flex: none; border-radius: 50%; color: var(--on-surface-variant);
}}
.icon-button:hover {{ background: var(--secondary-container); color: var(--on-secondary-container); }}
.empty {{ color: var(--on-surface-variant); }}
footer {{ max-width: 960px; margin: 0 auto; padding: 0 16px 32px; color: var(--on-surface-variant); font-size: .875rem; }}
@media (forced-colors: active) {{
  .card, .chip, .search input {{ border: 1px solid CanvasText; }}
}}
</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to flipbooks</a>
<header class="top-app-bar">
  <div class="inner">
    <h1>SCV Flipbook Archive</h1>
    <p>{count} PDF{plural}, newest first</p>
  </div>
</header>
<main id="main" tabindex="-1">
  <form class="search" role="search" onsubmit="return false">
    <label for="q">Search flipbooks</label>
    <div class="field">{icon_search}<input id="q" type="search" autocomplete="off" placeholder="Title, month or year"></div>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
  <nav class="year-nav" aria-label="Jump to year">
    <ul role="list">
{nav}
    </ul>
  </nav>
{sections}
</main>
<footer>
  <p>PDF copies of the flipbooks published on <a href="https://issuu.com/scvcommunications">Issuu by scvcommunications</a>.
  Files are stored in the <a href="https://github.com/{repo}/tree/main/PDF">{repo}</a> repository.
  Dates marked “estimated” were inferred from neighbouring issues.
  Updated <time datetime="{generated_iso}">{generated}</time>.</p>
</footer>
<script>
(() => {{
  const input = document.getElementById("q");
  const status = document.getElementById("status");
  const items = [...document.querySelectorAll(".item")];
  const sections = [...document.querySelectorAll(".year")];
  let timer;
  input.addEventListener("input", () => {{
    clearTimeout(timer);
    timer = setTimeout(() => {{
      const terms = input.value.toLowerCase().split(/\\s+/).filter(Boolean);
      let shown = 0;
      for (const item of items) {{
        const match = terms.every(t => item.dataset.search.includes(t));
        item.hidden = !match;
        if (match) shown++;
      }}
      for (const section of sections) {{
        const visible = section.querySelectorAll(".item:not([hidden])").length;
        section.hidden = visible === 0;
        section.querySelector(".count").textContent = `· ${{visible}}`;
        document.querySelector(`.chip[href="#${{section.id}}"]`).parentElement.hidden = visible === 0;
      }}
      status.textContent = terms.length ? `${{shown}} of ${{items.length}} flipbooks match.` : "";
    }}, 200);
  }});
}})();
</script>
</body>
</html>
"""


def main():
    links_path = sys.argv[1] if len(sys.argv) > 1 else "links.md"
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "PDF"
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "_site"
    items = load_pdfs(links_path, pdf_dir)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(items, datetime.now(timezone.utc)))
    print(f"Wrote {out_dir}/index.html listing {len(items)} PDFs.")


if __name__ == "__main__":
    main()
