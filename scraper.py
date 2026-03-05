"""
RAM price scraper — computerbase.de via Playwright headless Chromium.

computerbase.de price-comparison pages render their offer list entirely
client-side via a Geizhals JavaScript widget.  A real browser is required
to execute that JS and expose offer data in the DOM.

Extraction strategy
───────────────────
1. Open the computerbase.de URL in a headless Chromium page.
2. Wait for .gh_price elements to appear (main frame, then any child frames).
3. Collect offer rows; extract price (.gh_price) and shop (data-merchant-name).
4. Drop eBay and Amazon Marketplace offers (keep Amazon direct).
5. Return the lowest price and shop name.
"""

import logging
import random
import re
import time

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from database import init_db, insert_price
from models import MODULES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Max ms to wait for price elements after page load
OFFER_TIMEOUT_MS = 15_000


# ---------------------------------------------------------------------------
# Seller-exclusion logic
# ---------------------------------------------------------------------------

def _is_excluded_seller(name: str, href: str) -> bool:
    """
    Return True when the offer should be excluded.

    Rules:
    - eBay: always excluded.
    - Amazon Marketplace: excluded when 'amazon' appears in the name but the
      merchant href does NOT point to Amazon's own shop (/merchants/amazon).
    - Amazon direct (sold & shipped by Amazon): included.
    """
    name_lower = name.lower()
    href_lower = href.lower()

    if "ebay" in name_lower or "ebay" in href_lower:
        return True

    if "amazon" in name_lower or "amazon.de" in href_lower or "amazon.com" in href_lower:
        if "/merchants/amazon" in href_lower and "marketplace" not in name_lower:
            return False  # Amazon direct — keep
        return True  # Marketplace or ambiguous — skip

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

def _offers_from_context(context):
    """Return offer element handles from a Page or Frame, or []."""
    for selector in ("#lazy-list--offers .offer", ".offerlist .offer", ".offer"):
        els = context.query_selector_all(selector)
        if els:
            return els
    return []


def _dismiss_consent(page) -> bool:
    """
    Click through the computerbase.de GDPR consent dialog if present.
    Returns True if a consent button was clicked.
    """
    consent_selectors = [
        "#cookie-consent-button",           # computerbase.de primary
        ".js-consent-accept-button",        # computerbase.de fallback class
        "button:has-text('Akzeptieren und weiter')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Accept all')",
    ]
    for sel in consent_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                log.debug("Consent accepted via: %s", sel)
                return True
        except Exception:
            continue
    return False


def scrape_module(name: str, cb_url: str, page) -> tuple[float, str] | tuple[None, None]:
    """
    Navigate to cb_url and return (lowest_price, shop).
    Returns (None, None) on any error.
    """
    try:
        page.goto(cb_url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        log.error("[%s] Page load timed out", name)
        return None, None
    except Exception as exc:
        log.error("[%s] Page load error: %s", name, exc)
        return None, None

    # Wait 2s for the consent dialog to appear (it loads asynchronously)
    page.wait_for_timeout(2000)

    # Dismiss consent if present; if clicked, wait for the page to settle
    if _dismiss_consent(page):
        try:
            # "Akzeptieren und weiter" may trigger a full page reload
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

    # Scroll down so lazy-loaded widgets enter the viewport
    page.evaluate("window.scrollBy(0, 600)")
    page.wait_for_timeout(500)

    # The Geizhals widget may render in the main frame or inside an iframe.
    # Wait for .gh_price to appear in whichever context has it.
    target = None

    try:
        page.wait_for_selector(".gh_price", timeout=OFFER_TIMEOUT_MS)
        target = page
    except PWTimeout:
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                frame.wait_for_selector(".gh_price", timeout=5_000)
                target = frame
                break
            except PWTimeout:
                continue

    if target is None:
        # Check if the widget showed a rate-limit / error page
        error_text = page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        if "Fehler im Preisvergleich" in error_text:
            log.error("[%s] Geizhals rate-limit error page — try again later", name)
        else:
            log.warning("[%s] No price elements found after %dms", name, OFFER_TIMEOUT_MS)
        return None, None

    offers = _offers_from_context(target)
    if not offers:
        log.warning("[%s] No offer rows found in DOM", name)
        return None, None

    valid: list[tuple[float, str]] = []

    for offer in offers:
        # ── Price ──────────────────────────────────────────────────────────
        price_el = offer.query_selector(".gh_price")
        if not price_el:
            continue
        price = _parse_price(price_el.inner_text())
        if price is None:
            continue

        # ── Shop name ──────────────────────────────────────────────────────
        merchant_link = offer.query_selector("a[data-merchant-name]")
        if merchant_link:
            shop_name = merchant_link.get_attribute("data-merchant-name") or ""
            shop_href = merchant_link.get_attribute("href") or ""
        else:
            caption = offer.query_selector(".merchant__logo-caption")
            shop_name = caption.inner_text().strip() if caption else "Unknown"
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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_extra_http_headers({
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        for module in MODULES:
            name = module["name"]
            url = module["url"]

            price, shop = scrape_module(name, url, page)
            if price is not None:
                insert_price(name, url, price, shop)
            else:
                log.warning("[%s] Skipped — no price obtained", name)

            delay = random.uniform(4.0, 7.0)
            log.debug("Sleeping %.1fs", delay)
            time.sleep(delay)

        browser.close()

    try:
        from notifier import send_report
        send_report()
    except Exception as exc:
        log.error("Failed to send email report: %s", exc)


if __name__ == "__main__":
    main()
