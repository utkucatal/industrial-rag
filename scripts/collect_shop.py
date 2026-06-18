"""
Web shop collector — two stages:
  A) walk the listing (pagination) pages and gather product URLs
  B) open each product page and pull out structured fields
Output: catalog.json (+ catalog.jsonl from main()).

Category is not hardcoded — it comes from each product's breadcrumb.
No third-party deps (stdlib urllib + concurrent.futures).
"""
import os
import json
import re
import html
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# scripts/ lives one level below the repo root; write outputs to the root
ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
WORKERS = 8

# Shop base URL. Set SHOP_BASE_URL to point at the target; listing paths are relative to it.
BASE_URL = os.environ.get("SHOP_BASE_URL", "https://example-shop.com/")

# Listing meta (brand/name/price/id + url) gets embedded at the top of each saved HTML,
# so no separate manifest file is needed — every page carries its own meta.
META_RE = re.compile(r"<!--PRODUCT-META (.*?)-->")


def build_meta_header(meta: dict) -> str:
    return f"<!--PRODUCT-META {json.dumps(meta, ensure_ascii=False)}-->\n"


def extract_meta(page_html: str) -> dict:
    """Read the embedded listing meta from the top of an HTML page ({} if missing)."""
    m = META_RE.search(page_html[:4000])
    return json.loads(m.group(1)) if m else {}

# (path, page_count) — when page_count > 1 we append ?p=N&order=new-arrivals
LISTINGS = [
    ("sale", 3),
    ("machinery/components-standalone/bindery/coupling-boxes", 1),
    ("electrical/sensors/light-sensors", 5),
    ("electrical/boards", 5),
    ("mechanical/gears/straight-toothed", 5),
    ("electrical/electricity/motors", 10),
]


def fetch(url: str, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt == retries:
                print(f"  ! fetch fail: {url}  ({e})")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


# ---------- stage A: listing pages -> product links ----------

INFO_RE = re.compile(r'data-product-information="([^"]+)"')
# first product-name href that follows a data-product-information block
NAME_HREF_RE = re.compile(
    r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*product-name[^"]*"', re.I)


def listing_url(path: str, page: int, paged: bool) -> str:
    base = BASE_URL + path
    if not paged:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}p={page}&order=new-arrivals"


def collect_links() -> dict:
    """Return url -> {brand, name, listing_price, listing_id}."""
    found: dict[str, dict] = {}
    for path, pages in LISTINGS:
        paged = pages > 1
        for page in range(1, pages + 1):
            url = listing_url(path, page, paged)
            h = fetch(url)
            if not h:
                continue
            infos = [json.loads(html.unescape(m)) for m in INFO_RE.findall(h)]
            hrefs = [html.unescape(u) for u in NAME_HREF_RE.findall(h)]
            n_new = 0
            for d, href in zip(infos, hrefs):
                if href not in found:
                    found[href] = {
                        "listing_id": d.get("id"),
                        "name": d.get("name"),
                        "brand": d.get("brand"),
                        "listing_price": d.get("price"),
                    }
                    n_new += 1
            print(f"  [{path} p{page}] cards={len(infos)} new={n_new} total={len(found)}")
            if not infos:
                break  # empty page -> move on to the next path
    return found


# ---------- stage B: product page -> structured record ----------

OG_RE = re.compile(r'<meta property="og:(\w+)" content="([^"]*)"')
PRICE_RE = re.compile(r'itemprop="price"[^>]*content="([^"]+)"')
PRODID_RE = re.compile(r'productID" content="([^"]+)"')
ORDERNO_BLK = re.compile(r'product-detail-ordernumber.*?</div>', re.S)
OEM_BLK = re.compile(
    r'<span class="product-detail-manufacturer-number">(.*?)</span>', re.S)
BREADCRUMB_LIST = re.compile(r'schema\.org/BreadcrumbList')
ITEMNAME_RE = re.compile(r'itemprop="name"[^>]*>([^<]+)<')
DESC_BLK = re.compile(r'product-detail-description-text[^>]*>(.*?)</div>', re.S)
COND_PROP = re.compile(
    r'properties-label">\s*Condition:\s*</th>\s*<td[^>]*properties-value">(.*?)</td>', re.S)
COND_CFG = re.compile(r'product-detail-configurator-option-label[^>]*>\s*([A-Za-z]+)')

AVNUM_RE = re.compile(r'\bAV\w+\b')
DIM_RE = re.compile(r'dimensions?\s+([\d.,]+\s*x\s*[\d.,]+\s*x\s*[\d.,]+)\s*cm', re.I)
WEIGHT_RE = re.compile(r'(?:weighs|weight of|weighing)\s+([\d.,]+)\s*kg', re.I)
YEAR_RE = re.compile(r'\bYear[:\s]+(\d{4})\b', re.I)


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def to_float(num: str):
    try:
        return float(num.replace(",", ""))
    except (ValueError, AttributeError):
        return None


OEM_DESC_RE = re.compile(r'type number\s+([A-Za-z0-9./()\- ]+?)\s+from', re.I)


def parse_category(h: str, title: str = "") -> str | None:
    i = h.find("schema.org/BreadcrumbList")
    if i < 0:
        return None
    seg = h[i:i + 4000]
    names = [html.unescape(n).strip() for n in ITEMNAME_RE.findall(seg)]
    names = [n for n in names if n.lower() not in ("home", "% sale")]
    # last breadcrumb item is the product itself, not a category -> drop it
    if len(names) > 1:
        names = names[:-1]
    return " > ".join(names) if names else None


def parse_product(url: str, meta: dict, h: str) -> dict:
    og = dict(OG_RE.findall(h))
    title = html.unescape(og.get("title", meta.get("name") or "")).strip()
    desc_raw = DESC_BLK.search(h)
    desc = strip_tags(desc_raw.group(1)) if desc_raw else ""

    # order number (AV...)
    order_no = None
    blk = ORDERNO_BLK.search(h)
    if blk:
        m = AVNUM_RE.search(strip_tags(blk.group(0)))
        order_no = m.group(0) if m else None

    # OEM P/N
    oem = None
    om = OEM_BLK.search(h)
    if om:
        oem = strip_tags(om.group(1)).replace("OEM P/N:", "").strip() or None
    if not oem:  # fallback: "type number X from" in the description
        dm = OEM_DESC_RE.search(desc)
        oem = dm.group(1).strip() if dm else None

    # condition
    cond = None
    cm = COND_PROP.search(h) or COND_CFG.search(h)
    if cm:
        cond = strip_tags(cm.group(1)).title() or None

    price = PRICE_RE.search(h)
    dim = DIM_RE.search(desc)
    wt = WEIGHT_RE.search(desc)
    yr = YEAR_RE.search(desc)

    return {
        "id": order_no or meta.get("listing_id"),
        "title": title,
        "manufacturer": meta.get("brand"),
        "oem_pn": oem,
        "condition": cond,
        "price_eur": to_float(price.group(1)) if price else meta.get("listing_price"),
        "weight_kg": to_float(wt.group(1)) if wt else None,
        "dimensions_cm": re.sub(r"\s+", " ", dim.group(1)).strip() if dim else None,
        "year": int(yr.group(1)) if yr else None,
        "category": parse_category(h, title),
        "url": og.get("url") or url,
        "description": desc or html.unescape(og.get("description", "")),
        "shop_id": meta.get("listing_id"),
    }


def collect_product(url: str, meta: dict):
    h = fetch(url)
    if not h:
        return None
    try:
        return parse_product(url, meta, h)
    except Exception as e:  # don't let one bad page sink the whole run
        print(f"  ! parse fail: {url}  ({e})")
        return None


# ---------- main: fetch + parse in one pass (hits the site) ----------

def main():
    print("Stage A - collecting product links...")
    links = collect_links()
    print(f"\nUnique product URLs: {len(links)}\n")

    print("Stage B - fetching product pages...")
    products = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(collect_product, u, m): u for u, m in links.items()}
        for fut in as_completed(futs):
            rec = fut.result()
            done += 1
            if rec:
                products.append(rec)
            if done % 25 == 0 or done == len(links):
                print(f"  {done}/{len(links)} processed (ok={len(products)})")

    # sort by url for stable output
    products.sort(key=lambda r: r["url"])

    (ROOT / "catalog.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    with (ROOT / "catalog.jsonl").open("w", encoding="utf-8") as f:
        for r in products:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(products)} products -> catalog.json / catalog.jsonl")
    keys = ["id", "manufacturer", "oem_pn", "condition", "price_eur",
            "category", "weight_kg", "dimensions_cm", "year"]
    print("--- field coverage ---")
    for k in keys:
        filled = sum(1 for r in products if r.get(k) not in (None, ""))
        print(f"  {k:14} {filled}/{len(products)}")


if __name__ == "__main__":
    main()
