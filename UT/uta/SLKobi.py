import math
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
    ChromeDriver build newer than the actually installed Chrome, which
    breaks the session entirely - pin it to the real installed major
    version instead. Same as DC.py's helper of the same name. Tries
    macOS's fixed app path first, then whatever Chrome/Chromium binary
    is on PATH (e.g. Linux CI runners)."""
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


# Set up Chrome options
chrome_options = uc.ChromeOptions()
chrome_options.headless = False
# Don't block driver.get() on images/ads/trackers finishing to load -
# the WebDriverWait below already waits for the specific elements we need.
chrome_options.page_load_strategy = "eager"
chrome_options.add_argument("--window-size=1920,1080")

# Initialize the Chrome driver
chrome_major = detect_chrome_major_version()
driver = uc.Chrome(options=chrome_options, version_main=chrome_major) if chrome_major else uc.Chrome(options=chrome_options)
# undetected_chromedriver swaps out its initial blank tab as part of its own
# startup patching - calling .get() before that settles can raise
# NoSuchWindowException ("target window already closed") because the
# session's current window still points at the tab it just closed.
# Confirmed by hand: reproduces on the very first navigation of any site,
# not just this one. Switching to whatever window handle is actually live
# avoids it.
driver.switch_to.window(driver.window_handles[-1])

# Legacy.com's own county-level browse page - the same URL pattern works for
# every county in the country (.../local/<state>/<county>-county), with the
# date_range query param picking which window of results the site itself
# filters down to server-side, so there's no client-side date parsing to do.
OBITUARY_URL = 'https://www.legacy.com/us/obituaries/local/utah/utah-county'
DATE_RANGE_BY_MODE = {"daily": "today", "weekly": "last_week"}
obituary_url = f"{OBITUARY_URL}?date_range={DATE_RANGE_BY_MODE[MODE]}"

# Each result is an <article data-component="PersonCardContainer"> - found
# by inspecting the live site after a redesign broke the old absolute-
# position XPath this used to use. Every card actually has *two* links
# with the same "View obituary details for <Name>" aria-label (one wraps
# the text, one wraps the thumbnail photo) - confirmed by hand - so this
# has to look up the name scoped to one card at a time (driver.find_element,
# singular, only ever returns the first match) rather than a single flat
# query for every matching link on the page, or every name gets counted
# twice.
CARD_XPATH = '//article[@data-component="PersonCardContainer"]'
NAME_LINK_XPATH = './/a[starts-with(@aria-label, "View obituary details for")]'
NAME_LINK_PREFIX = "View obituary details for "

# Total-results count shown above the list (e.g. "143 Results") - the site
# paginates at exactly 50 per page, so this tells us how many more pages
# (?page=2, ?page=3, ...) to walk through beyond the first. Matched by the
# stable "N Results" wording rather than a position/class, for the same
# reason as NAME_LINK_XPATH above.
RESULT_COUNT_XPATH = '//div[contains(text(), "Results")]'
RESULTS_PER_PAGE = 50


def parse_total_results(text):
    """Pull the total result count out of whatever the count element says
    (e.g. "143 results" or "Showing 1-50 of 143") - the largest number in
    it is always the total, regardless of exact wording."""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", text or "")]
    return max(numbers) if numbers else None


# This will hold all the obituary (name, posted_date) pairs
all_entries = []

try:
    # Open the obituary page directly
    print(f"Opening obituary page: {obituary_url}")
    driver.get(obituary_url)

    try:
        count_element = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, RESULT_COUNT_XPATH))
        )
        total_results = parse_total_results(count_element.text)
    except Exception:
        total_results = None

    total_pages = math.ceil(total_results / RESULTS_PER_PAGE) if total_results else 1
    print(f"Extracting obituary names ({MODE} mode) - {total_results or '?'} result(s) across {total_pages} page(s)...")

    for page in range(1, total_pages + 1):
        if page > 1:
            page_url = f"{obituary_url}&page={page}"
            print(f"Opening page {page}: {page_url}")
            driver.get(page_url)

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, CARD_XPATH))
            )
        except Exception:
            print(f"No obituary entries found on page {page}.")
            continue

        # All 50 (or fewer, on the last page) cards are already in the DOM -
        # no scrolling/lazy-loading needed to get at them.
        page_names = 0
        for card in driver.find_elements(By.XPATH, CARD_XPATH):
            try:
                link = card.find_element(By.XPATH, NAME_LINK_XPATH)
            except Exception:
                continue
            label = link.get_attribute("aria-label") or ""
            name = label[len(NAME_LINK_PREFIX):].strip() if label.startswith(NAME_LINK_PREFIX) else ""
            if name:
                # The date_range query param already limits results to the
                # window we care about, so every name gets tagged with
                # today's date - there's no per-entry date header on this
                # page to read a more precise one from.
                all_entries.append((name, TODAY.isoformat()))
                page_names += 1
        print(f"  Page {page}: found {page_names} name(s).")

    print(f"Found {len(all_entries)} names total.")

except Exception as e:
    print(f"Error during execution: {e}")

finally:
    driver.quit()

# Cache the names for SLK$ copy.py to pick up
if not all_entries:
    print("No obituary names found.")
else:
    pipeline_db.replace_obituary_names(all_entries)
    print(f"Cached {len(all_entries)} obituary names in {pipeline_db.DB_PATH}")
