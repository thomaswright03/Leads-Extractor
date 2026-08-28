import json
import os
import re
import shutil
import subprocess
import sys
import time

import pandas as pd
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Written by DC.py at the end of its run - lists exactly the file(s) it just
# produced (one "today" file in daily mode, or one per posted date in weekly
# mode), so pricer.py doesn't have to guess from today's date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

PARCEL_BOX_XPATH = "/html/body/section[3]/div/div/main/section[1]/div[2]/div[1]/form/p[4]/input"
SEARCH_BUTTON_XPATH = "/html/body/section[3]/div/div/main/section[1]/div[2]/div[1]/form/button[1]"
PROPERTY_VALUE_XPATH = "/html/body/section[4]/div/div/main/section/div/div[2]/div[1]/div[2]/div/table/tbody/tr[10]/td[2]"


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
    # Don't block driver.get() on images/ads/trackers finishing to load -
    # the WebDriverWait calls below already wait for the specific elements needed.
    options.page_load_strategy = "eager"
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


def get_property_value(driver, parcel_id):
    driver.get("https://www.saltlakecounty.gov/assessor/")
    original_window = driver.current_window_handle

    parcel_box = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, PARCEL_BOX_XPATH))
    )
    parcel_box.clear()
    parcel_box.send_keys(parcel_id)

    search_button = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
    time.sleep(0.5)
    search_button.click()

    # A parcel ID search redirects straight to the results page in the
    # same tab (confirmed live - no new window ever opens for that case).
    # An ambiguous match could still open a second tab, the way the
    # owner-name search flow does elsewhere in this pipeline, so wait for
    # whichever happens rather than assuming one or the other.
    value_located = EC.presence_of_element_located((By.XPATH, PROPERTY_VALUE_XPATH))
    WebDriverWait(driver, 20).until(lambda d: len(d.window_handles) > 1 or value_located(d))

    opened_new_tab = len(driver.window_handles) > 1
    if opened_new_tab:
        new_window = [h for h in driver.window_handles if h != original_window][0]
        driver.switch_to.window(new_window)

    try:
        value_el = WebDriverWait(driver, 20).until(value_located)
        return value_el.text.strip()
    finally:
        if opened_new_tab:
            driver.close()
            driver.switch_to.window(original_window)


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
            # Fresh browser window for every address - close it out and
            # start clean rather than reusing one session across lookups.
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

        df["Property Value"] = property_values
        df.to_excel(matches_path, index=False)
        print(f"Updated {matches_path} with {len(property_values)} property value(s).")
