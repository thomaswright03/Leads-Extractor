import json
import os
import random
import re
import shutil
import subprocess
import sys
import time

import pandas as pd
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Written by DC.py at the end of its run - lists exactly the file(s) it just
# produced, so pricer.py doesn't have to guess from today's date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

# Directly bookmarkable by parcel id - confirmed live, no re-search needed.
ACCOUNT_URL = "https://webercountyutah.gov/parcelsearch/current-taxes.php?id={parcel_id}"
# Confirmed live: this page's "Parcel Summary" box has three <strong> labels
# (Market, Taxable, Rate) each immediately followed by its value in the same
# parent <div> - Market is the one this pipeline wants everywhere else.
MARKET_LABEL = "Market"


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


def get_last_run():
    """The mode and exact file(s) DC.py produced on its last run, per its
    manifest - this is what actually determines what there is to price,
    regardless of which mode is chosen when running pricer.py stand-alone."""
    if not os.path.exists(LAST_RUN_PATH):
        return None, []
    with open(LAST_RUN_PATH) as f:
        data = json.load(f)
    paths = [p for p in data.get("files", []) if os.path.exists(p)]
    return data.get("mode"), paths


def create_driver():
    options = uc.ChromeOptions()
    options.headless = False
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1920,1080")
    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)
    # undetected_chromedriver swaps out its initial blank tab as part of its
    # own startup patching - calling .get() before that settles can raise
    # NoSuchWindowException ("target window already closed"), confirmed
    # elsewhere in this pipeline. Since every lookup here gets a brand-new
    # driver, every single lookup is a "first navigation", so this matters
    # on every call, not just once per run.
    driver.switch_to.window(driver.window_handles[-1])
    return driver


def lookup_property_value(driver, parcel_id):
    driver.get(ACCOUNT_URL.format(parcel_id=parcel_id))
    WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")

    for strong in driver.find_elements(By.TAG_NAME, "strong"):
        if strong.text.strip() != MARKET_LABEL:
            continue
        # Confirmed live: the value sits as a bare text node right after a
        # <br> inside the same parent <div> as the "Market" <strong> label -
        # .text on that parent div reads as "Market\n$1,118,700.00".
        parent_text = strong.find_element(By.XPATH, "..").text
        match = re.search(r"\$[\d,]+(?:\.\d+)?", parent_text)
        if match:
            return match.group(0)
    return None


if __name__ == "__main__":
    mode = pipeline_db.resolve_mode(sys.argv)
    last_run_mode, matches_paths = get_last_run()

    if not matches_paths:
        print("No DC.py output found to price. Run DC.py first.")
        raise SystemExit(0)

    if last_run_mode and last_run_mode != mode:
        print(f"Note: DC.py's last run was '{last_run_mode}' mode - pricing those {len(matches_paths)} file(s) regardless of the mode picked here.")

    for matches_path in matches_paths:
        df = pd.read_excel(matches_path)
        if df.empty:
            print(f"{matches_path} has no matches - nothing to price.")
            continue

        property_values = []
        for _, row in df.iterrows():
            parcel_id = str(row.get("Parcel ID", "")).strip()
            full_name = row.get("Full Name", "")
            address, city, state, zip_code = pipeline_db.get_address_by_parcel_id(parcel_id)

            if not address:
                print(f"No address on file for '{full_name}' (Parcel ID {parcel_id}) - skipping.")
                property_values.append("No address on file")
                continue

            print(f"Looking up property value for '{full_name}': Parcel ID {parcel_id}")
            # Fresh browser window for every lookup - simpler than reusing
            # one session across every row, and a crashed/stuck browser
            # only costs one lookup instead of the whole file.
            driver = create_driver()
            try:
                value = lookup_property_value(driver, parcel_id)
                property_values.append(value or "Not found")
                print(f"  -> {value or 'Not found'}")
            except Exception as e:
                print(f"  -> Failed to get property value: {e}")
                property_values.append("Not found")
            finally:
                driver.quit()

            # Looks less like a bot hammering the site back-to-back.
            time.sleep(random.uniform(1.5, 3.5))

        df["Property Value"] = property_values
        df.to_excel(matches_path, index=False)
        print(f"Updated {matches_path} with {len(property_values)} property value(s).")
