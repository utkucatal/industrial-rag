"""
Stage 2 — offline, never touches the site.
  - reads pages/*.html
  - pulls url + brand from the embedded meta (<!--PRODUCT-META ...-->) at the top of each file
  - builds a structured record with parse_product
  - writes catalog.json
No manifest needed — the meta lives inside every HTML file.
Tweak the parser and re-run as often as you like; the site is never hit again.
"""
import json
from pathlib import Path

import collect_shop as s  # parse_product, extract_meta (no network)

ROOT = Path(__file__).resolve().parent.parent   # repo root (scripts/ is one level down)
PAGES = ROOT / "pages"


def main():
    files = sorted(PAGES.glob("*.html"))
    if not files:
        raise SystemExit("pages/ is empty. Run python fetch_pages.py first.")

    products = []
    no_meta = 0
    for fpath in files:
        h = fpath.read_text(encoding="utf-8")
        meta = s.extract_meta(h)
        if not meta:
            no_meta += 1
        url = meta.get("url") or fpath.stem
        try:
            products.append(s.parse_product(url, meta, h))
        except Exception as e:
            print(f"  ! parse fail: {fpath.name}  ({e})")

    products.sort(key=lambda r: r["url"])

    (ROOT / "catalog.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Parsed {len(products)} products -> catalog.json")
    if no_meta:
        print(f"  (warning: {no_meta} files had no embedded meta — re-download with fetch_pages.py)")

    keys = ["id", "manufacturer", "oem_pn", "condition", "price_eur",
            "category", "weight_kg", "dimensions_cm", "year"]
    print("--- field coverage ---")
    for k in keys:
        filled = sum(1 for r in products if r.get(k) not in (None, ""))
        print(f"  {k:14} {filled}/{len(products)}")


if __name__ == "__main__":
    main()
