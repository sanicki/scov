# scvcommunications flipbook archive

Archives the flipbooks published at https://issuu.com/scvcommunications.

- **`.github/workflows/fetch-links.yml`** runs daily (and on demand). It runs
  `scripts/fetch_links.py` to collect every publication URL into `links.md` on
  `main`. Links are only ever added, never removed.
- **`.github/workflows/download-pdfs.yml`** runs when `links.md` changes on
  `main`. It runs `scripts/download_pdfs.py` to build a PDF in `PDF/` for every
  link that doesn't have one yet, 10 per run by default. It starts another run
  while a backlog remains.

Every flipbook gets a consistent name, such as "Tipster – September 2026",
"Divots – October 2019" or "Tee to Green – March 2021", built by
`scripts/naming.py` from its Issuu title and doc name. One-off publications keep
their own title with a normalised date. When an issue was uploaded more than
once, only the latest upload is kept in `links.md`, since earlier copies may
contain mistakes; its PDF is replaced and the older copy's PDF deleted.
To fix a name by hand, add the doc name to `OVERRIDES` in `scripts/naming.py`.

PDFs are named after the flipbook's name in `links.md` (with the Issuu doc name
appended when two flipbooks share a title). `PDF/manifest.json` maps each link
to its file, so a PDF is renamed rather than downloaded again if its title changes.

- **`.github/workflows/pages.yml`** publishes the archive to GitHub Pages. It
  runs `scripts/build_site.py` to list every PDF, newest first, by the date in
  its title. Before deploying, `scripts/a11y-check.mjs` audits the page with
  axe-core (WCAG 2.2 AA) in light and dark mode at desktop and phone widths;
  any violation fails the deploy. It runs after every PDF download run.

PDFs that would exceed GitHub's 100 MB file limit even after recompression are
listed in `PDF/skipped.md` and not retried.

Run locally:

```sh
pip install -r requirements.txt
python scripts/fetch_links.py scvcommunications links.md
python scripts/download_pdfs.py links.md PDF 10
```
