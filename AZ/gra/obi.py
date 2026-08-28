import re
import shutil
import subprocess
import sys
from datetime import datetime

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

MODE = pipeline_db.resolve_mode(sys.argv)
TODAY = datetime.now().date()

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]


def detect_chrome_major_version():
    """undetected_chromedriver's own version auto-detection can grab a
    ChromeDriver build that's newer than the actually installed Chrome
    (seen going for 151 while Chrome was still on 150), which breaks the
    session entirely. Pin it to the real installed major version instead.
    Tries macOS's fixed app path first, then whatever Chrome/Chromium
    binary is on PATH (e.g. Linux CI runners)."""
    for candidate in CHROME_BINARY_CANDIDATES:
        binary = shutil.which(candidate)
        if not binary:
            continue
        try:
            output = subprocess.check_output([binary, "--version"], text=True)
            match = re.search(r"(\d+)\.", output)
            if match:
                return int(match.group(1))
        except Exception:
            continue
    return None


# Set up Chrome options - undetected_chromedriver, not plain webdriver.Chrome(),
# since Legacy.com (this script's target) is already known to need it
# elsewhere in this pipeline (see DC.py's identical per-name searches there).
chrome_options = uc.ChromeOptions()
# Don't block driver.get() on images/ads/trackers finishing to load -
# the WebDriverWait below already waits for the specific elements we need.
chrome_options.page_load_strategy = "eager"
# chrome_options.add_argument("--headless=new")  # Uncomment if you want headless mode
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

# Initialize the Chrome driver
_chrome_major = detect_chrome_major_version()
driver = uc.Chrome(options=chrome_options, version_main=_chrome_major) if _chrome_major else uc.Chrome(options=chrome_options)
# undetected_chromedriver swaps out its initial blank tab as part of its
# own startup patching - calling .get() before that settles can raise
# NoSuchWindowException ("target window already closed"), confirmed live.
# Switching to whatever window handle is actually live avoids it (same fix
# UT/uta's equivalent script already uses).
driver.switch_to.window(driver.window_handles[-1])

# Legacy.com's own county-level browse page - the same URL pattern works for
# every county in the country (.../local/<state>/<county>-county), with the
# date_range query param picking which window of results the site itself
# filters down to server-side, so there's no client-side date parsing to do.
OBITUARY_URL = 'https://www.legacy.com/us/obituaries/local/arizona/graham-county'
DATE_RANGE_BY_MODE = {"daily": "today", "weekly": "last_week"}
obituary_url = f"{OBITUARY_URL}?date_range={DATE_RANGE_BY_MODE[MODE]}"

# Each obituary card is an <article data-component="PersonCardContainer">
# (confirmed live - a prior site redesign broke the old absolute-XPath
# version of this, which silently matched zero elements). The card's own
# link carries the person's name right in its aria-label ("View obituary
# details for <name>"), which is far more stable than reading visible text
# out of a CSS-class div that's subject to the site's next redesign.
CARD_SELECTOR = 'article[data-component="PersonCardContainer"]'
NAME_LINK_SELECTOR = 'a[aria-label^="View obituary details for "]'
NAME_LINK_PREFIX = "View obituary details for "

# The site doesn't expose a reliably-selectable "total results" count
# element, so page until a page comes back with fewer than a full page of
# cards (confirmed live: 50/page, last page short) rather than precomputing
# a page count. MAX_PAGES is just a runaway safety cap.
RESULTS_PER_PAGE = 50
MAX_PAGES = 6


# This will hold all the obituary (name, posted_date) pairs
all_entries = []

try:
    # Open the obituary page directly
    print(f"Opening obituary page: {obituary_url}")
    driver.get(obituary_url)

    print(f"Extracting obituary names ({MODE} mode)...")

    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            page_url = f"{obituary_url}&page={page}"
            print(f"Opening page {page}: {page_url}")
            driver.get(page_url)

        try:
            # Wait for the name link itself (not just the card container) -
            # the cards mount before React hydrates their content, so
            # waiting on the container alone raced ahead of the aria-label
            # actually being populated and silently produced 0 names on an
            # otherwise full page (confirmed live).
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"{CARD_SELECTOR} {NAME_LINK_SELECTOR}"))
            )
        except Exception:
            print(f"No obituary entries found on page {page}.")
            break

        # All cards are already in the DOM - no scrolling/lazy-loading needed.
        cards = driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)
        page_names = 0
        for card in cards:
            try:
                link = card.find_element(By.CSS_SELECTOR, NAME_LINK_SELECTOR)
                name = link.get_attribute("aria-label").replace(NAME_LINK_PREFIX, "", 1).strip()
            except Exception:
                continue
            if name:
                # The date_range query param already limits results to the
                # window we care about, so every name gets tagged with
                # today's date - there's no per-entry date header on this
                # page to read a more precise one from.
                all_entries.append((name, TODAY.isoformat()))
                page_names += 1
        print(f"  Page {page}: found {page_names} name(s).")

        if len(cards) < RESULTS_PER_PAGE:
            break  # short page - this was the last one

    print(f"Found {len(all_entries)} names total.")

except Exception as e:
    print(f"Error during execution: {e}")

finally:
    driver.quit()

# Cache the names for $.py to pick up
if not all_entries:
    print("No obituary names found.")
else:
    pipeline_db.replace_obituary_names(all_entries)
    print(f"Cached {len(all_entries)} obituary names in {pipeline_db.DB_PATH}")
