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

def _is_excluded_seller(name: str, merchant_url: str = "") -> bool:
    """
    Return True when the offer should be excluded.

    Rules:
    - eBay: always excluded. Detected via:
        1. 'ebay' in the shop name (catches offers labelled 'eBay')
        2. '-eb-de' or '-eb-com' in the computerbase merchant profile URL
           (catches individual eBay seller accounts like 'bernis-products',
            whose profile URL ends in e.g. 'bernis-products-46-eb-de')
    - Amazon Marketplace (third-party sellers on amazon.de): excluded.
      Detected via:
        1. 'amazon' AND 'marketplace' in the shop name (legacy fallback)
        2. '-am-de' or '-am-com' in the computerbase merchant profile URL
           (third-party sellers have a URL like '{seller-id}-am-de',
            e.g. 'a2pgpjl0bblhlx-am-de' for AnkerDirect)
    - Amazon direct (merchant URL 'amazon-de', shop name 'Amazon.de'): included.
    """
    name_lower = name.lower()
    url_lower = merchant_url.lower()

    # eBay: by shop name
    if "ebay" in name_lower:
        return True

    # eBay: by merchant profile URL suffix (-eb-de / -eb-com) or explicit domain
    if "-eb-de" in url_lower or "-eb-com" in url_lower:
        return True
    if "ebay.de" in url_lower or "ebay.com" in url_lower:
        return True

    # Amazon Marketplace: by shop name (legacy fallback)
    if "amazon" in name_lower and "marketplace" in name_lower:
        return True

    # Amazon Marketplace: by merchant profile URL suffix
    # Note: Amazon direct uses '.../merchants/amazon-de' which does NOT match '-am-de'
    if "-am-de" in url_lower or "-am-com" in url_lower:
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

        # ── Merchant profile URL — used to detect eBay / Amazon Marketplace ──
        # The href may be absolute (https://www.computerbase.de/…) or relative
        # (/preisvergleich/merchants/…) depending on what requests receives.
        # Matching on '/merchants/' works for both forms.
        cb_merchant_link = offer.select_one(
            ".offer__merchant-info-links a[href*='/merchants/']"
        )
        merchant_url = cb_merchant_link.get("href", "") if cb_merchant_link else ""

        # ── Exclusion filter ───────────────────────────────────────────────
        if _is_excluded_seller(shop_name, merchant_url):
            log.info("[%s] Excluding: %s (merchant_url=%s)", name, shop_name, merchant_url)
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
