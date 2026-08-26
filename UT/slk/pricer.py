import json
import os
import random
import sys
import time

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

# These absolute paths into the search form are the same class of bug fixed
# in SLK$.py: confirmed live that the site was restructured and these all
# now match zero elements, while the form fields' own ids still exist and
# are visible - switched to id-based lookups the same way. PROPERTY_VALUE_XPATH
# is on a different page template that wasn't affected (confirmed live it
# still resolves to a real value), so it's left as-is.
PARCEL_BOX_ID = "ParcelSearch"
ADDRESS_BOX_ID = "AddressSearch"
SEARCH_BUTTON_ID = "BeginSearch"
PROPERTY_VALUE_XPATH = "/html/body/section[4]/div/div/main/section/div/div[2]/div[1]/div[2]/div/table/tbody/tr[10]/td[2]"
# The autocomplete dropdown is jQuery UI's own ui-autocomplete widget, not
# site-specific markup - confirmed live typing a real address into
# #AddressSearch populates exactly this structure.
AUTOCOMPLETE_SELECTOR = "ul.ui-autocomplete li.ui-menu-item div"


def get_last_run():
    if not os.path.exists(LAST_RUN_PATH):
        return None, []
    with open(LAST_RUN_PATH) as f:
        data = json.load(f)
    paths = [p for p in data.get("files", []) if os.path.exists(p)]
    return data.get("mode"), paths


def create_driver(eager=True):
    options = webdriver.ChromeOptions()
    if eager:
        # Don't block driver.get() on images/ads/trackers finishing to load -
        # the WebDriverWait calls below already wait for the specific elements needed.
        options.page_load_strategy = "eager"
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # get_property_value() below can also land on a page that opens a new
    # window for ambiguous matches - Chrome's popup blocker was the real
    # cause of SLK$.py's 100% failure rate (it silently ate the
    # window.open() the search button fires), so this script is exposed to
    # the same risk the moment it hits that same ambiguous-match path.
    options.add_argument("--disable-popup-blocking")
    # Plain webdriver.Chrome() lets Selenium's own built-in driver manager
    # handle the chromedriver binary, instead of webdriver_manager's
    # separate ChromeDriverManager().install().
    return webdriver.Chrome(options=options)


def get_property_value(driver, parcel_id):
    driver.get("https://www.saltlakecounty.gov/assessor/")
    original_window = driver.current_window_handle

    parcel_box = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.ID, PARCEL_BOX_ID))
    )
    parcel_box.clear()
    parcel_box.send_keys(parcel_id)

    search_button = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, SEARCH_BUTTON_ID))
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
        driver.get("https://www.saltlakecounty.gov/assessor/")
        address_box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, ADDRESS_BOX_ID))
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
                    result_el = driver.find_element(By.CSS_SELECTOR, AUTOCOMPLETE_SELECTOR)
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
