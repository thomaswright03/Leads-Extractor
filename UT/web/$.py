import re
import shutil
import subprocess
import sys
from urllib.parse import quote_plus

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import geocode
import pipeline_db

MODE = pipeline_db.resolve_mode(sys.argv)

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Weber County's parcel search is a plain GET with the last name in the
# query string - confirmed live, no form interaction needed at all. The
# results table (id="results", a DataTables grid) already shows every
# matching row on one page with no further clicking - confirmed live with
# a 945-row "Smith" search returning in full immediately, so unlike Utah
# County's equivalent search there's no pagination to walk here.
SEARCH_URL = "https://webercountyutah.gov/parcelsearch/results.php"

# Generational suffixes to skip when splitting a full name, and
# parenthetical nicknames (e.g. "Jacob (Jake) Smith") to strip before
# splitting.
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
PAREN_PATTERN = re.compile(r"\([^)]*\)")

chrome_options = uc.ChromeOptions()
chrome_options.headless = False
chrome_options.page_load_strategy = "eager"
chrome_options.add_argument("--window-size=1920,1080")


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


def _strip_suffix_clause(full_name):
    """Obituary listing names occasionally carry a trailing rank/title
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") that isn't
    part of the actual name - left in, it gets treated as the last word and
    searched as a last name that doesn't exist. A comma-separated
    generational suffix (e.g. "Robert Smith, Jr.") is kept, since that's
    part of the actual name."""
    name, sep, suffix = (full_name or "").partition(",")
    if sep and suffix.strip().rstrip(".").lower() not in SUFFIXES:
        return name.strip()
    return (full_name or "").strip()


def split_name(full_name):
    """Deceased's full name -> (last_name, first_name)."""
    cleaned = PAREN_PATTERN.sub("", _strip_suffix_clause(full_name)).strip()
    words = [w for w in cleaned.split() if w.strip(".").lower() not in SUFFIXES]
    if len(words) < 2:
        return "", ""
    return words[-1], words[0]


def build_search_url(last_name):
    return f"{SEARCH_URL}?type=name&name={quote_plus(last_name)}"


# The address column bundles street and city with no separator (e.g.
# "660 27TH ST OGDEN") - confirmed live. Rather than guess where the street
# ends and the city begins, hand the whole thing to geocode.geocode_address
# as the street query and let it resolve city/state/zip from Google's own
# match - same approach this pipeline uses wherever a county doesn't cleanly
# split the two itself.
def extract_rows(driver):
    """Pull every result row's (owner_name, parcel_id, raw_address) as plain
    text before anything (geocoding) can navigate the driver away and turn
    these into stale elements. Confirmed live: table#results tbody tr, with
    td order parcel/owner/address/status/view-link."""
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "table#results tbody tr"):
        cells = tr.find_elements(By.TAG_NAME, "td")
        if len(cells) < 3:
            continue  # the "No data available in table" placeholder row
        parcel_id = cells[0].text.strip()
        owner_name = cells[1].text.strip()
        raw_address = cells[2].text.strip()
        if not parcel_id:
            continue
        rows.append((owner_name, parcel_id, raw_address))
    return rows


def search_last_name(driver, last_name, first_name, full_name, posted_date):
    match_count = 0
    driver.get(build_search_url(last_name))
    # page_load_strategy="eager" can return control before the DOM is
    # actually queryable - confirmed on this same pattern elsewhere in this
    # pipeline (see UT/uta's equivalent script). Waiting for
    # readyState=="complete" fixes it.
    WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")

    for owner_name, parcel_id, raw_address in extract_rows(driver):
        if not any(word.lower().startswith(first_name.lower()) for word in owner_name.split()):
            continue
        city, state, zip_code = geocode.geocode_address(driver, raw_address)
        pipeline_db.add_match(
            full_name, owner_name, parcel_id, raw_address, city, state, zip_code, posted_date
        )
        match_count += 1
        print(f"  Match: owner '{owner_name}' / parcel {parcel_id}")

    return match_count


if __name__ == "__main__":
    pipeline_db.clear_matches()

    names = pipeline_db.get_obituary_names_with_dates()
    print(f"Searching Weber County assessor records for {len(names)} name(s)...")

    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=chrome_options, version_main=chrome_major) if chrome_major else uc.Chrome(options=chrome_options)
    # undetected_chromedriver swaps out its initial blank tab as part of its
    # own startup patching - calling .get() before that settles can raise
    # NoSuchWindowException ("target window already closed"), confirmed
    # elsewhere in this pipeline on the very first navigation of any site.
    # Switching to whatever window handle is actually live avoids it.
    driver.switch_to.window(driver.window_handles[-1])
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
