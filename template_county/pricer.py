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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

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

ASSESSOR_BASE_URL = __ASSESSOR_BASE_URL__
PARCEL_BOX_XPATH = __PARCEL_BOX_XPATH__
ADDRESS_BOX_XPATH = __ADDRESS_BOX_XPATH__
SEARCH_BUTTON_XPATH = __SEARCH_BUTTON_XPATH__
PROPERTY_VALUE_XPATH = __PROPERTY_VALUE_XPATH__
AUTOCOMPLETE_XPATH = __AUTOCOMPLETE_XPATH__
# If True, skip hunting for the search button entirely and just press Enter
# after typing into the search box - some assessor sites submit the form on
# Enter and don't have (or don't need) a stable button XPath.
SEARCH_SUBMIT_VIA_ENTER = __SEARCH_SUBMIT_VIA_ENTER__


def submit_search(driver, box_element):
    """Submit the search form either by pressing Enter in the box just typed
    into, or by finding and clicking the search button - whichever this
    county's site was configured to need."""
    if SEARCH_SUBMIT_VIA_ENTER:
        box_element.send_keys(Keys.RETURN)
        return
    search_button = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
    time.sleep(0.5)
    search_button.click()


def get_last_run():
    if not os.path.exists(LAST_RUN_PATH):
        return None, []
    with open(LAST_RUN_PATH) as f:
        data = json.load(f)
    paths = [p for p in data.get("files", []) if os.path.exists(p)]
    return data.get("mode"), paths


def create_driver(eager=True):
    # undetected_chromedriver, not plain webdriver.Chrome() - for consistency
    # with every other driver-creating script in this pipeline. This also
    # drops the separate webdriver_manager/ChromeDriverManager().install()
    # dependency, which has been seen failing every single call in CI
    # (confirmed live for UT/slk's equivalent script) - undetected_chromedriver
    # manages its own driver binary instead.
    options = uc.ChromeOptions()
    if eager:
        # Don't block driver.get() on images/ads/trackers finishing to load -
        # the WebDriverWait calls below already wait for the specific elements needed.
        options.page_load_strategy = "eager"
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    chrome_major = detect_chrome_major_version()
    return uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)


def get_property_value(driver, parcel_id):
    driver.get(ASSESSOR_BASE_URL)
    original_window = driver.current_window_handle

    parcel_box = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, PARCEL_BOX_XPATH))
    )
    parcel_box.clear()
    parcel_box.send_keys(parcel_id)

    submit_search(driver, parcel_box)

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


def parse_autocomplete_result(text):
    """Split 'STREET, CITY, ZIP' - what the assessor's own address-search
    autocomplete shows - into (city, zip). Returns (None, None) if the text
    doesn't look like that three-part format."""
    parts = [p.strip() for p in (text or "").split(",")]
    if len(parts) != 3:
        return None, None
    _, city, zip_code = parts
    return city or None, zip_code or None


def fill_city_zip(matches_paths):
    """Second pass: fill in City/Zip for every row across all matches_paths
    using the assessor's own address-search autocomplete instead of an
    external geocoder. The assessor's autocomplete is backed by their own
    complete address database, so unlike the free Census geocoder (which
    relies on interpolated street-address ranges with real coverage gaps),
    it has no gaps to hit.

    One page load for the whole pass, not eager - the address box's
    autocomplete widget needs its JS fully initialized before typing into
    it. Each address is typed into the same box in place (cleared and
    retyped), with a hard wait for the dropdown, no reloads in between."""
    print("\nFilling in City/Zip from the assessor's address autocomplete...")
    driver = create_driver(eager=False)
    try:
        driver.get(ASSESSOR_BASE_URL)
        address_box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, ADDRESS_BOX_XPATH))
        )

        for matches_path in matches_paths:
            df = pd.read_excel(matches_path)
            if df.empty or "Address" not in df.columns:
                continue
            # Avoid pandas' dtype-coercion warning when writing strings into
            # a City/Zip column pandas inferred as float64 (all-blank cells).
            df["City"] = df["City"].astype(object)
            df["Zip"] = df["Zip"].astype(object)

            updated = False
            for idx, row in df.iterrows():
                address = str(row.get("Address", "")).strip()
                if not address or address.lower() == "nan":
                    continue

                # The unit/apartment number (e.g. "# 9") is irrelevant to
                # which city/zip the address falls in, and confuses the
                # autocomplete match - search on the street address alone.
                search_address = address.split("#")[0].strip()

                address_box.clear()
                address_box.send_keys(search_address)
                time.sleep(2)

                try:
                    result_el = driver.find_element(By.XPATH, AUTOCOMPLETE_XPATH)
                    city, zip_code = parse_autocomplete_result(result_el.text)
                except Exception:
                    city, zip_code = None, None

                print(f"  {address} -> city={city or '?'}, zip={zip_code or '?'}")

                if city:
                    df.at[idx, "City"] = city
                    updated = True
                if zip_code:
                    df.at[idx, "Zip"] = zip_code
                    updated = True

            if updated:
                df.to_excel(matches_path, index=False)
                print(f"Updated {matches_path} with City/Zip.")
    finally:
        driver.quit()


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

            # Small human-like pause between lookups.
            time.sleep(random.uniform(2, 4))

        df["Property Value"] = property_values
        df.to_excel(matches_path, index=False)
        print(f"Updated {matches_path} with {len(property_values)} property value(s).")

    fill_city_zip(matches_paths)
