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

# Written by DC.py at the end of its run - lists exactly the file(s) it just
# produced (one "today" file in daily mode, or one per posted date in weekly
# mode), so this script doesn't have to guess from today's date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

INQUIRY_URL = "https://gcwebinfo.graham.az.gov/Production/RealWareDataMartPropertyInquiryWebsite/Inquiry.aspx"


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


def create_driver():
    # undetected_chromedriver, not plain webdriver.Chrome(), for consistency
    # with every other driver-creating script in this pipeline.
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)
    # undetected_chromedriver swaps out its initial blank tab as part of its
    # own startup patching - calling .get() before that settles can raise
    # NoSuchWindowException ("target window already closed"), confirmed
    # live. Switching to whatever window handle is actually live avoids it
    # (same fix UT/uta's equivalent script already uses).
    driver.switch_to.window(driver.window_handles[-1])
    return driver


def _is_single_result_page(driver):
    return bool(driver.find_elements(By.XPATH, "//td[normalize-space(text())='Owner Name & Address']"))


def _no_results(driver):
    return "did not match any parcels" in driver.page_source


def get_property_value(driver, parcel_id):
    driver.get(INQUIRY_URL)
    WebDriverWait(driver, 30).until(lambda d: d.find_elements(By.ID, "ctl00_ContentPlaceHolder1_AcctNumberTxt"))
    # This site remembers every search field server-side across requests in
    # the same session (confirmed live) - a fresh driver per lookup (below)
    # sidesteps that here, but clearing directly is cheap insurance either
    # way and avoids depending on that being true forever.
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctOwnerTxt").clear()
    field = driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctNumberTxt")
    field.clear()
    field.send_keys(parcel_id)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_SearchBtn").click()
    WebDriverWait(driver, 30).until(lambda d: _no_results(d) or _is_single_result_page(d))

    if _no_results(driver):
        raise ValueError(f"Account {parcel_id} not found on assessor site.")

    # "Value Information"'s own Totals row, not "Limited Property Value
    # (LPV)"'s - the page has two identically-labeled Totals rows
    # (confirmed live), and this is always the first one in the DOM.
    totals_row = driver.find_element(By.XPATH, "//strong[normalize-space(text())='Totals:']/ancestor::tr[1]")
    cells = totals_row.find_elements(By.TAG_NAME, "td")
    value = cells[2].text.strip() if len(cells) > 2 else ""
    if not value:
        raise ValueError(f"Account {parcel_id}: value column never populated.")
    return value


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
            # Fresh browser window for every parcel - close it out and start
            # clean rather than reusing one session across lookups, so a
            # crashed/stuck browser only costs one lookup instead of the
            # whole file.
            driver = create_driver()
            try:
                value = get_property_value(driver, parcel_id)
                print(f"  -> {value}")
                property_values.append(value)
            except Exception as e:
                print(f"  -> Failed to get property value: {e}")
                property_values.append("Not found")
            finally:
                driver.quit()

            time.sleep(random.uniform(1.5, 3.5))

        df["Property Value"] = property_values
        df.to_excel(matches_path, index=False)
        print(f"Updated {matches_path} with {len(property_values)} property value(s).")
