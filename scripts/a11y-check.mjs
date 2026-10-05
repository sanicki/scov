// Audits the built site with axe-core (WCAG 2.2 A/AA plus best practices) in
// light and dark mode, at desktop and phone widths. Exits non-zero on any violation.
//
// Usage: node scripts/a11y-check.mjs [_site/index.html]
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const axeSource = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const page_url = pathToFileURL(resolve(process.argv[2] ?? "_site/index.html")).href;

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
let failures = 0;
const configs = [];
for (const colorScheme of ["light", "dark"]) {
  for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 740 }]) {
    configs.push({ colorScheme, viewport, search: "" });
  }
}
// Also audit the page while a search filter is applied.
configs.push({ colorScheme: "light", viewport: { width: 1280, height: 900 }, search: "2021" });

for (const { colorScheme, viewport, search } of configs) {
  const page = await browser.newPage({ colorScheme, viewport });
  // Fonts load from Google; the audit doesn't depend on them.
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (route) => route.abort());
  await page.goto(page_url);
  if (search) {
    await page.fill("#q", search);
    await page.waitForTimeout(500);
  }
  await page.addScriptTag({ content: axeSource });
  const results = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
    }),
  );
  const label = `${colorScheme} ${viewport.width}px${search ? ` searching "${search}"` : ""}`;
  if (results.violations.length === 0) {
    console.log(`✓ ${label}: no violations (${results.passes.length} rules passed)`);
  }
  for (const v of results.violations) {
    failures++;
    console.log(`✗ ${label}: [${v.impact}] ${v.id} — ${v.help} (${v.nodes.length} element(s))`);
    for (const node of v.nodes.slice(0, 3)) console.log(`    ${node.target.join(" ")}: ${node.failureSummary}`);
  }
  await page.close();
}
await browser.close();
process.exit(failures ? 1 : 0);
