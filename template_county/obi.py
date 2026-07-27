import math
import re
import sys
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

import pipeline_db

MODE = pipeline_db.resolve_mode(sys.argv)
TODAY = datetime.now().date()

# Set up Chrome options
chrome_options = Options()
# Don't block driver.get() on images/ads/trackers finishing to load -
# the WebDriverWait below already waits for the specific elements we need.
chrome_options.page_load_strategy = "eager"
# chrome_options.add_argument("--headless=new")  # Uncomment if you want headless mode
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

# Initialize the Chrome driver
driver = webdriver.Chrome(options=chrome_options)

# Legacy.com's own county-level browse page - the same URL pattern works for
# every county in the country (.../local/<state>/<county>-county), with the
# date_range query param picking which window of results the site itself
# filters down to server-side, so there's no client-side date parsing to do.
OBITUARY_URL = __OBITUARY_URL__
DATE_RANGE_BY_MODE = {"daily": "today", "weekly": "last_week"}
obituary_url = f"{OBITUARY_URL}?date_range={DATE_RANGE_BY_MODE[MODE]}"

# Every obituary card on the page is an <article> at this same parent path;
# dropping the [N] index off "article" matches all of them in one XPath.
NAME_XPATH = "/html/body/div[4]/div/div[2]/div/div[2]/div/article/div[1]/div/a/div/div"

# Total-results count shown above the list (e.g. "143 results") - the site
# paginates at exactly 50 per page, so this tells us how many more pages
# (?page=2, ?page=3, ...) to walk through beyond the first.
RESULT_COUNT_XPATH = "/html/body/div[4]/div/div[2]/div/div[1]/div[1]"
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
                EC.presence_of_element_located((By.XPATH, NAME_XPATH))
            )
        except Exception:
            print(f"No obituary entries found on page {page}.")
            continue

        # All 50 (or fewer, on the last page) cards are already in the DOM -
        # no scrolling/lazy-loading needed to get at them.
        page_names = 0
        for element in driver.find_elements(By.XPATH, NAME_XPATH):
            name = element.text.strip()
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

# Cache the names for $.py to pick up
if not all_entries:
    print("No obituary names found.")
else:
    pipeline_db.replace_obituary_names(all_entries)
    print(f"Cached {len(all_entries)} obituary names in {pipeline_db.DB_PATH}")
