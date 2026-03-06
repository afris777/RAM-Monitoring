"""
RAM price scraper — computerbase.de via plain HTTP.

computerbase.de price-comparison pages are fully server-side rendered:
the offer list (prices + shop names) is present in the raw HTML response.
No browser, no JavaScript, no Geizhals API calls required.

Extraction strategy
───────────────────
1. GET the computerbase.de URL with a browser-like User-Agent.
2. Parse .offer rows from the HTML with BeautifulSoup.
3. Extract price (.gh_price) and shop name (a[data-merchant-name]).
4. Drop eBay and Amazon Marketplace offers (keep Amazon direct).
5. Return the lowest price and shop name.
"""

import logging
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from database import init_db, insert_price
from models import MODULES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Seller-exclusion logic
# ---------------------------------------------------------------------------

def _is_excluded_seller(name: str) -> bool:
    """
    Return True when the offer should be excluded.

    Rules:
    - eBay: always excluded.
    - Amazon Marketplace: excluded when 'amazon' and 'marketplace' both
      appear in the name (e.g. "Amazon Marketplace").
    - Amazon direct (Amazon.de, sold & shipped by Amazon): included.
    """
    name_lower = name.lower()

    if "ebay" in name_lower:
        return True

    if "amazon" in name_lower and "marketplace" in name_lower:
        return True

    return False


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> float | None:
    """Convert '€ 419,90' → 419.9."""
    cleaned = re.sub(r"[^\d,]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def scrape_module(name: str, cb_url: str) -> tuple[float, str] | tuple[None, None]:
    """
    Fetch cb_url and return (lowest_price, shop).
    Returns (None, None) on any error.
    """
    try:
        resp = SESSION.get(cb_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("[%s] HTTP error: %s", name, exc)
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")
    offers = soup.select(".offer")

    if not offers:
        log.warning("[%s] No offer rows found in HTML", name)
        return None, None

    valid: list[tuple[float, str]] = []

    for offer in offers:
        # ── Price ──────────────────────────────────────────────────────────
        price_el = offer.select_one(".gh_price")
        if not price_el:
            continue
        price = _parse_price(price_el.get_text())
        if price is None:
            continue

        # ── Shop name ──────────────────────────────────────────────────────
        merchant_link = offer.select_one("a[data-merchant-name]")
        if merchant_link:
            shop_name = merchant_link.get("data-merchant-name") or ""
        else:
            caption = offer.select_one(".merchant__logo-caption")
            shop_name = caption.get_text(strip=True) if caption else "Unknown"

        # ── Exclusion filter ───────────────────────────────────────────────
        if _is_excluded_seller(shop_name):
            log.debug("[%s] Excluding: %s", name, shop_name)
            continue

        valid.append((price, shop_name))

    if not valid:
        log.warning("[%s] No valid offers after filtering (%d total)", name, len(offers))
        return None, None

    valid.sort(key=lambda x: x[0])
    lowest_price, cheapest_shop = valid[0]
    log.info(
        "[%s] €%.2f at %s  (%d offers, %d excluded)",
        name, lowest_price, cheapest_shop, len(offers), len(offers) - len(valid),
    )
    return lowest_price, cheapest_shop


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()

    for module in MODULES:
        name = module["name"]
        url = module["url"]

        price, shop = scrape_module(name, url)
        if price is not None:
            insert_price(name, url, price, shop)
        else:
            log.warning("[%s] Skipped — no price obtained", name)

        delay = random.uniform(1.5, 3.0)
        log.debug("Sleeping %.1fs", delay)
        time.sleep(delay)

    try:
        from notifier import send_report
        send_report()
    except Exception as exc:
        log.error("Failed to send email report: %s", exc)


if __name__ == "__main__":
    main()
