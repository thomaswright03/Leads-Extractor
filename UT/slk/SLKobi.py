import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

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


def parse_header_date(label, today):
    """Turn a page date header ('Today', 'Yesterday', or 'Monday, July 20,
    2026') into a date object, or None if it isn't a recognized date header."""
    label = (label or "").strip()
    if not label:
        return None
    lower = label.lower()
    if lower == "today":
        return today
    if lower == "yesterday":
        return today - timedelta(days=1)
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(label, fmt).date()
        except ValueError:
            continue
    return None


MODE = pipeline_db.resolve_mode(sys.argv)
TODAY = datetime.now().date()

# Set up Chrome options - undetected_chromedriver, not plain webdriver.Chrome(),
# for consistency with every other driver-creating script in this pipeline.
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

# Target obituary page
obituary_url = "https://www.legacy.com/us/obituaries/saltlaketribune/browse?view=name"

# This will hold all the obituary (name, posted_date) pairs
all_entries = []

try:
    # Open the obituary page directly
    print(f"Opening obituary page: {obituary_url}")
    driver.get(obituary_url)

    # The site groups obituaries into small batches and re-prints the date
    # label at the start of each batch, so e.g. "Today" can appear more than
    # once as a separate header in the results list. Walk the flat list of
    # siblings (date labels + name cards) and collect every name that falls
    # under a header in the date range we care about (just today in daily
    # mode, or today and the previous 6 days in weekly mode).
    print(f"Extracting obituary names ({MODE} mode)...")
    try:
        anchor_label = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.XPATH, "//p[normalize-space(text())='Today' or normalize-space(text())='Yesterday' or contains(text(), ', 20')]")
            )
        )
    except Exception:
        anchor_label = None

    if anchor_label is None:
        print("No obituary date headers found.")
    else:
        container = anchor_label.find_element(By.XPATH, "..")
        current_date = None
        for child in container.find_elements(By.XPATH, "./*"):
            if child.tag_name.lower() == "p":
                current_date = parse_header_date(child.text.strip(), TODAY)
                continue
            if current_date is None:
                continue
            days_ago = (TODAY - current_date).days
            in_range = days_ago == 0 if MODE == "daily" else 0 <= days_ago <= 6
            if not in_range:
                continue
            try:
                name = child.find_element(By.TAG_NAME, "h6").text.strip()
            except Exception:
                name = ""
            if name:
                all_entries.append((name, current_date.isoformat()))

    print(f"Found {len(all_entries)} names.")

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
