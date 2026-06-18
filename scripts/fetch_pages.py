"""
Stage 1 — runs once, hits the site once.
  - collects product URLs from the listing (pagination) pages
  - saves each product page's HTML into pages/
  - embeds the listing meta (url/brand/name/price/id) at the top of each file
    (<!--PRODUCT-META {...}-->), so no separate manifest is needed.
Does not parse. To turn the HTML into data, run parse_pages.py (offline).

Re-running skips already-downloaded HTML (idempotent) and only fetches what's missing.
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import collect_shop as s  # collect_links, fetch, build_meta_header, WORKERS

ROOT = Path(__file__).resolve().parent.parent   # repo root (scripts/ is one level down)
PAGES = ROOT / "pages"


def slugify(url: str) -> str:
    """Turn a URL into a safe file name."""
    slug = re.sub(r"^https?://[^/]+/", "", url)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug).strip("_")
    return (slug[:150] or "index") + ".html"


def download_one(url: str, meta: dict) -> str | None:
    """Download the page, prepend the listing-meta header, save it. Returns the url."""
    fpath = PAGES / slugify(url)
    if fpath.exists() and fpath.stat().st_size > 1000:
        return url  # already have it, skip
    h = s.fetch(url)
    if not h:
        return None
    header = s.build_meta_header({"url": url, **meta})
    fpath.write_text(header + h, encoding="utf-8")
    return url


def main():
    PAGES.mkdir(exist_ok=True)

    print("Collecting product links from listing pages...")
    links = s.collect_links()
    print(f"\nUnique product URLs: {len(links)}\n")

    print(f"Downloading HTML -> {PAGES}/  (meta header embedded, existing files skipped)")
    done = saved = 0
    with ThreadPoolExecutor(max_workers=s.WORKERS) as ex:
        futs = {ex.submit(download_one, u, m): u for u, m in links.items()}
        for fut in as_completed(futs):
            if fut.result():
                saved += 1
            done += 1
            if done % 25 == 0 or done == len(links):
                print(f"  {done}/{len(links)} (ok={saved})")

    on_disk = len(list(PAGES.glob("*.html")))
    print(f"\nHTML files in pages/: {on_disk}")
    print("Next: python parse_pages.py  (offline parse)")


if __name__ == "__main__":
    main()
