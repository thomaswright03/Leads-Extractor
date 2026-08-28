import shutil
import subprocess
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
import time
import re
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urlparse, parse_qs

import pipeline_db
import geocode

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]


def detect_chrome_major_version():
    """undetected_chromedriver's own version auto-detection can grab a
    ChromeDriver build that's newer than the actually installed Chrome,
    which breaks the session entirely. Pin it to the real installed major
    version instead - same fix DC.py uses. Tries macOS's fixed app path
    first, then whatever Chrome/Chromium binary is on PATH (e.g. Linux CI
    runners)."""
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
OWNER_SEARCH_XPATH = __OWNER_SEARCH_XPATH__
SEARCH_BUTTON_XPATH = __SEARCH_BUTTON_XPATH__
# If True, skip hunting for the search button entirely and just press Enter
# after typing into the search box - some assessor sites submit the form on
# Enter and don't have (or don't need) a stable button XPath.
SEARCH_SUBMIT_VIA_ENTER = __SEARCH_SUBMIT_VIA_ENTER__
NO_RESULTS_XPATH = __NO_RESULTS_XPATH__
NO_RESULTS_TEXT = __NO_RESULTS_TEXT__
# "N/A" (case-insensitive) if this county's site never redirects a single
# match straight to its own details page - every search then always lands on
# the multi-result list below, even for exactly one match.
PARCEL_DETAILS_HEADER_TEXT = __PARCEL_DETAILS_HEADER_TEXT__
OWNER_LABEL_TEXT = __OWNER_LABEL_TEXT__
ADDRESS_LABEL_TEXT = __ADDRESS_LABEL_TEXT__
# Each of these matches every result row at once (drop the [N] index off one
# example row's XPath, same trick as obi.py's NAME_XPATH) - the three
# lists are expected to line up 1:1 in the same order.
RESULT_OWNER_XPATH = __RESULT_OWNER_XPATH__
RESULT_PARCEL_XPATH = __RESULT_PARCEL_XPATH__
RESULT_ADDRESS_XPATH = __RESULT_ADDRESS_XPATH__

# Load the names to search from the shared cache (populated by obi.py)
names_with_dates = pipeline_db.get_obituary_names_with_dates()

# Start this run's match cache fresh
pipeline_db.clear_matches()

print("Names to search:")
for name, posted_date in names_with_dates:
    print(f"{name} ({posted_date})" if posted_date else name)

def extract_last_name(full_name):
    name_parts = full_name.strip().split()
    if name_parts[-1].lower() in ['jr.', 'sr.']:
        return name_parts[-2]
    name_parts = re.sub(r'\(.*\)$', '', ' '.join(name_parts)).split()
    return name_parts[-1]

def submit_search(driver, box_element):
    """Submit the search form either by pressing Enter in the box just typed
    into, or by finding and clicking the search button - whichever this
    county's site was configured to need."""
    if SEARCH_SUBMIT_VIA_ENTER:
        box_element.send_keys(Keys.RETURN)
        return
    search_button = WebDriverWait(driver, 20).until(
        EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
    time.sleep(0.5)
    search_button.click()


def extract_first_name(full_name):
    name_parts = full_name.strip().split()
    if len(name_parts) > 1:
        return name_parts[0].capitalize()
    return ''


def record_if_match(driver, name, first_name, posted_date, owner_name, parcel_id, address):
    owner_name_parts = owner_name.split()
    if not any(word.lower().startswith(first_name.lower()) for word in owner_name_parts):
        return None
    # Navigates driver to Google to read the zip code - only safe to call
    # once every row's text has already been pulled off the results page (see
    # the multi-result loop below), since this leaves that page.
    city, state, zip_code = geocode.geocode_address(driver, address)
    print(f"Match found for {name} with owner: {owner_name}, address: {address}")
    pipeline_db.add_match(name, owner_name, parcel_id, address, city, state, zip_code, posted_date)
    return {
        "Full Name": name,
        "Owner Name": owner_name,
        "Parcel ID": parcel_id,
        "Address": address,
        "City": city,
        "State": state,
        "Zip": zip_code,
    }


results = []

for index, (name, posted_date) in enumerate(names_with_dates):
    last_name = extract_last_name(name)
    first_name = extract_first_name(name)

    options = uc.ChromeOptions()
    options.headless = False
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)
    # undetected_chromedriver swaps out its initial blank tab as part of its
    # own startup patching - calling .get() before that settles can raise
    # NoSuchWindowException ("target window already closed"), confirmed
    # live. Switching to whatever window handle is actually live avoids it
    # (same fix UT/uta's equivalent script already uses).
    driver.switch_to.window(driver.window_handles[-1])

    try:
        driver.get(ASSESSOR_BASE_URL)
        time.sleep(0.5)

        print(f"Searching for last name: {last_name}")

        search_box_owner = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, OWNER_SEARCH_XPATH))
        )
        search_box_owner.clear()
        search_box_owner.send_keys(last_name)

        submit_search(driver, search_box_owner)

        time.sleep(3)

        WebDriverWait(driver, 20).until(lambda d: len(d.window_handles) > 1)
        driver.switch_to.window(driver.window_handles[-1])

        # Added section: check for no results or single result page

        try:
            no_results_element = driver.find_element(By.XPATH, NO_RESULTS_XPATH)
            no_results_text = no_results_element.text.strip()
            if NO_RESULTS_TEXT in no_results_text:
                print(f"No results found for {name}. Moving on.")
                driver.quit()
                continue
        except:
            pass

        if PARCEL_DETAILS_HEADER_TEXT.strip().upper() != "N/A":
            try:
                h2_elements = driver.find_elements(By.TAG_NAME, "h2")
                header_text = h2_elements[0].text.strip() if h2_elements else ""
                if header_text == PARCEL_DETAILS_HEADER_TEXT:
                    # The parcel details page (the assessor site's own parcel-detail URL)
                    # renders as plain unlabeled divs/table rows rather than the nested
                    # table structure the old absolute XPaths assumed, so pull the
                    # parcel id straight from the URL and match the owner row by its
                    # label text instead of a brittle fixed path.
                    parcel_id = parse_qs(urlparse(driver.current_url).query).get("parcel_id", [""])[0]

                    owner_name = ""
                    address = ""
                    for tr in driver.find_elements(By.TAG_NAME, "tr"):
                        cells = tr.find_elements(By.TAG_NAME, "td")
                        if len(cells) < 2:
                            continue
                        label = cells[0].text.strip().lower()
                        if label == OWNER_LABEL_TEXT:
                            owner_name = cells[1].text.strip()
                        elif label == ADDRESS_LABEL_TEXT:
                            address = cells[1].text.strip()

                    match = record_if_match(driver, name, first_name, posted_date, owner_name, parcel_id, address)
                    if match:
                        print(f"Single result found for {name}: Owner = {owner_name}, Parcel ID = {parcel_id}, Address = {address}")
                        results.append(match)
                    else:
                        print(f"Single result found for {name} but owner '{owner_name}' doesn't match first name '{first_name}'; skipping.")
                    driver.quit()
                    continue
            except:
                pass

        # Multiple results - read them straight off the page, no file to
        # download. Each RESULT_*_XPATH matches every row at once, so the
        # three lists should line up 1:1 in the same order.
        owner_elements = driver.find_elements(By.XPATH, RESULT_OWNER_XPATH)
        parcel_elements = driver.find_elements(By.XPATH, RESULT_PARCEL_XPATH)
        address_elements = driver.find_elements(By.XPATH, RESULT_ADDRESS_XPATH)

        if len({len(owner_elements), len(parcel_elements), len(address_elements)}) > 1:
            print(f"Warning: mismatched result counts for '{name}' "
                  f"(owner={len(owner_elements)}, parcel={len(parcel_elements)}, address={len(address_elements)}) "
                  "- rows may not line up correctly.")

        # Pull every row's text off the page up front - record_if_match()
        # below navigates this same driver away to Google to look up each
        # match's zip code, which would invalidate the remaining element
        # handles here if done mid-loop.
        rows = [
            (owner_el.text.strip(), parcel_el.text.strip(), address_el.text.strip())
            for owner_el, parcel_el, address_el in zip(owner_elements, parcel_elements, address_elements)
        ]

        for owner_name, parcel_id, address in rows:
            match = record_if_match(driver, name, first_name, posted_date, owner_name, parcel_id, address)
            if match:
                results.append(match)

    except Exception as e:
        print(f"Error during search for {last_name}: {e}")

    driver.quit()

# Results are already cached in pipeline_db as they were found.
# Run DC.py after this to confirm matches and produce the final Excel file.
print("Final results cached:")
print(results)
