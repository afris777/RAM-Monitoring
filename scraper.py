"""
RAM price scraper for computerbase.de price-comparison pages.

Architecture
────────────
computerbase.de embeds a Geizhals (gzhls.at) widget that renders offers
client-side via JavaScript.  The raw server HTML returned to plain HTTP clients
has no offer data, so we bypass it entirely and go to the underlying Geizhals
source page instead.

Article IDs in the computerbase.de URLs (e.g. …-a3164911.html) map 1-to-1 to
Geizhals product pages:
    https://geizhals.at/eu/a3164911.html
    → redirects to https://geizhals.eu/…-a3164911.html

The Geizhals page is fully server-side-rendered HTML — no JS required.

Extraction strategy
───────────────────
1. Derive the Geizhals URL from the article ID in the computerbase.de URL.
2. Fetch with requests (browser User-Agent).
3. Parse the offer list (#lazy-list--offers .offer) with BeautifulSoup.
4. Drop eBay and Amazon Marketplace offers (keep Amazon direct).
5. Parse price from .gh_price text; shop name from data-merchant-name attr.
6. Return the lowest price and shop name.
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEIZHALS_BASE = "https://geizhals.at/eu/"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(REQUEST_HEADERS)


# ---------------------------------------------------------------------------
# Seller-exclusion logic
# ---------------------------------------------------------------------------

def _is_excluded_seller(name: str, href: str) -> bool:
    """
    Return True when the offer should be excluded.

    Rules:
    - eBay: always excluded.
    - Amazon Marketplace: excluded when 'amazon' appears in the name but the
      merchant href does NOT point to Amazon's own shop (.../merchants/amazon...).
    - Amazon direct (sold & shipped by Amazon): included.
    """
    name_lower = name.lower()
    href_lower = href.lower()

    if "ebay" in name_lower or "ebay" in href_lower:
        return True

    if "amazon" in name_lower or "amazon.de" in href_lower or "amazon.com" in href_lower:
        # Official Amazon shop: href contains /merchants/amazon (geizhals pattern)
        if "/merchants/amazon" in href_lower and "marketplace" not in name_lower:
            return False  # Amazon direct — keep
        return True  # Amazon Marketplace or ambiguous — skip

    return False


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _article_id(cb_url: str) -> str | None:
    """Extract the article ID (e.g. 'a3164911') from a computerbase.de URL."""
    m = re.search(r"-(a\d+)\.html", cb_url)
    return m.group(1) if m else None


def _geizhals_url(cb_url: str) -> str | None:
    """Build the Geizhals fetch URL for a given computerbase.de product URL."""
    aid = _article_id(cb_url)
    return f"{GEIZHALS_BASE}{aid}.html" if aid else None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> float | None:
    """Convert '€ 419,90' → 419.9."""
    cleaned = re.sub(r"[^\d,]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def scrape_module(name: str, cb_url: str) -> tuple[float, str] | tuple[None, None]:
    """
    Fetch the Geizhals page for the product and return (lowest_price, shop).
    Returns (None, None) on any error.
    """
    gh_url = _geizhals_url(cb_url)
    if not gh_url:
        log.error("[%s] Cannot build Geizhals URL from: %s", name, cb_url)
        return None, None

    try:
        resp = SESSION.get(gh_url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("[%s] Request failed: %s", name, exc)
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")

    # The main offer list — deliberately narrow selector to exclude variant
    # dropdown widgets and other price references outside the offer table.
    offers = (
        soup.select("#lazy-list--offers .offer")
        or soup.select(".offerlist .offer")
    )

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
            shop_name = merchant_link["data-merchant-name"]
            shop_href = merchant_link.get("href", "")
        else:
            caption = offer.select_one(".merchant__logo-caption")
            shop_name = caption.get_text(strip=True) if caption else "Unknown"
            shop_href = ""

        # ── Exclusion filter ───────────────────────────────────────────────
        if _is_excluded_seller(shop_name, shop_href):
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

        # Polite delay between requests
        delay = random.uniform(2.0, 3.5)
        log.debug("Sleeping %.1fs", delay)
        time.sleep(delay)

    # Send the email report after all prices are stored
    try:
        from notifier import send_report
        send_report()
    except Exception as exc:
        log.error("Failed to send email report: %s", exc)


if __name__ == "__main__":
    main()
