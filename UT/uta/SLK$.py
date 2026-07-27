import re
import sys
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

import geocode
import pipeline_db

MODE = pipeline_db.resolve_mode(sys.argv)

SEARCH_URL = "https://www.utahcounty.gov/LandRecords/NameSearch.asp"
# "Current Records Only" - the site's own default radio selection, and the
# one that matches what this pipeline wants (who owns the property now).
AV_VALID_CURRENT = "..."
RESULTS_PER_PAGE = 100

# Jr./Sr. suffixes to skip when splitting a full name, and parenthetical
# nicknames (e.g. "Jacob (Jake) Smith") to strip before splitting.
SUFFIXES = {"jr", "sr"}
PAREN_PATTERN = re.compile(r"\([^)]*\)")

chrome_options = Options()
chrome_options.page_load_strategy = "eager"
# chrome_options.add_argument("--headless=new")  # Uncomment if you want headless mode
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")


def split_name(full_name):
    """Deceased's full name -> (last_name, first_name)."""
    cleaned = PAREN_PATTERN.sub("", full_name or "").strip()
    words = [w for w in cleaned.split() if w.strip(".").lower() not in SUFFIXES]
    if len(words) < 2:
        return "", ""
    return words[-1], words[0]


def build_search_url(last_name, offset=0):
    url = f"{SEARCH_URL}?av_name={quote_plus(last_name)}&av_valid={quote_plus(AV_VALID_CURRENT)}"
    if offset:
        url += f"&offset={offset}"
    return url


def split_address(raw_address):
    """The assessor's address column bundles street and city as
    "STREET - CITY" - split them so geocode.geocode_address gets a real
    city instead of falling back to the county-wide default. Rows with no
    " - " (no situs address on file) are left with no usable street."""
    raw_address = (raw_address or "").strip()
    if " - " in raw_address:
        street, city = raw_address.split(" - ", 1)
        return street.strip(), city.strip()
    return raw_address, None


def extract_rows(driver):
    """Pull every result row's (owner_name, parcel_id, raw_address) as plain
    text before anything (geocoding) can navigate the driver away and turn
    these into stale elements."""
    rows = []
    for tr in driver.find_elements(By.XPATH, "//tr[td/a[contains(@href, 'av_serial=')]]"):
        cells = tr.find_elements(By.TAG_NAME, "td")
        owner_name = cells[0].text.strip()
        parcel_id = cells[1].text.strip()
        raw_address = cells[-1].text.strip()
        rows.append((owner_name, parcel_id, raw_address))
    return rows


def has_next_page(driver):
    return bool(driver.find_elements(By.XPATH, "//a[normalize-space(text())='Next']"))


def search_last_name(driver, last_name, first_name, full_name, posted_date):
    match_count = 0
    offset = 0

    while True:
        driver.get(build_search_url(last_name, offset))

        rows = extract_rows(driver)
        if not rows:
            break  # no results on this page (or none at all, if offset == 0)

        next_page = has_next_page(driver)  # read before any geocoding navigates away

        for owner_name, parcel_id, raw_address in rows:
            if not any(word.lower().startswith(first_name.lower()) for word in owner_name.split()):
                continue
            street, city = split_address(raw_address)
            resolved_city, state, zip_code = geocode.geocode_address(driver, street, city=city)
            pipeline_db.add_match(
                full_name, owner_name, parcel_id, street, resolved_city, state, zip_code, posted_date
            )
            match_count += 1
            print(f"  Match: owner '{owner_name}' / parcel {parcel_id}")

        if not next_page:
            break
        offset += RESULTS_PER_PAGE

    return match_count


if __name__ == "__main__":
    pipeline_db.clear_matches()

    names = pipeline_db.get_obituary_names_with_dates()
    print(f"Searching Utah County assessor records for {len(names)} name(s)...")

    driver = webdriver.Chrome(options=chrome_options)
    total_matches = 0

    try:
        for full_name, posted_date in names:
            last_name, first_name = split_name(full_name)
            if not last_name or not first_name:
                print(f"Skipping '{full_name}' - couldn't split into first/last name.")
                continue

            print(f"Searching for '{full_name}' (last name: {last_name})...")
            try:
                found = search_last_name(driver, last_name, first_name, full_name, posted_date)
                total_matches += found
                if not found:
                    print("  No matches.")
            except Exception as e:
                print(f"Error searching for '{full_name}': {e}")
                continue
    finally:
        driver.quit()

    print(f"\nDone. Found {total_matches} candidate match(es) across {len(names)} name(s).")
