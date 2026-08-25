import re
import sys
from urllib.parse import quote_plus, urlparse, parse_qs

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db
import geocode

# mcassessor.maricopa.gov's own help page (home/help/searching/) says to
# search owner name with the FULL name, not last-name-only: its single
# search box otherwise fuzzy-matches the query against addresses and
# subdivisions too - searching just "Johnson" pulls in ~8,800 rows, most of
# them unrelated parcels that merely sit on a street named Johnson.
# Searching "First Last" together cuts that down to a couple dozen at most.
SEARCH_URL = "https://mcassessor.maricopa.gov/mcs/?q={query}&mod=sm&search-type=rp&page={page}"
RESULTS_PER_PAGE = 25
MAX_PAGES = 4  # safety cap - 100 rows is already generous for a two-word name match

ZIP_RE = re.compile(r"\b(\d{5})\b")


def create_driver():
    # Plain webdriver.Chrome() lets Selenium's own built-in driver manager
    # handle the chromedriver binary - webdriver_manager's separate
    # ChromeDriverManager().install() has been seen failing every single
    # call in CI (confirmed live for UT/slk's equivalent script), so this
    # avoids that whole failure mode rather than working around it.
    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _strip_suffix_clause(full_name):
    """Obituary listing names occasionally carry a trailing rank/title
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") - confirmed
    live to corrupt last-name extraction (grabbing "Col/R" instead of
    "Kitchin"), which degrades the search into matching almost any owner
    with that first name. A comma-separated generational suffix (e.g.
    "Robert Smith, Jr.") is kept, since that's part of the actual name."""
    name, sep, suffix = full_name.partition(",")
    if sep and suffix.strip().rstrip(".").lower() not in GENERATIONAL_SUFFIXES:
        return name.strip()
    return full_name.strip()


def extract_last_name(full_name):
    name_parts = _strip_suffix_clause(full_name).split()
    while name_parts and name_parts[-1].lower().rstrip(".") in GENERATIONAL_SUFFIXES:
        name_parts = name_parts[:-1]
    name_parts = re.sub(r"\(.*?\)", "", " ".join(name_parts)).split()
    return name_parts[-1] if name_parts else ""


def extract_first_name(full_name):
    name_parts = re.sub(r"\(.*?\)", "", _strip_suffix_clause(full_name)).split()
    return name_parts[0] if name_parts else ""


def parse_total_results(text):
    """Handles all three phrasings this page uses: 'Showing 1 to 25 of 28
    results' (>25 total), 'Showing 2 results' (<=25, no 'of X'), and
    'Showing 0 results'."""
    text = text or ""
    of_match = re.search(r"of ([\d,]+) results", text)
    if of_match:
        return int(of_match.group(1).replace(",", ""))
    showing_match = re.search(r"Showing (\d+) results", text)
    if showing_match:
        return int(showing_match.group(1))
    return None


def parse_rows(driver):
    """Pull every real-property result row on the current page as plain
    data before anything else touches this driver - geocode_address() below
    navigates it away to Google, which would stale-element any rows read
    after that point (see $.md)."""
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
        cells = tr.find_elements(By.TAG_NAME, "td")
        if len(cells) < 3:
            continue  # spacer row, or the single-cell "No parcel records found." row
        apn_links = cells[0].find_elements(By.TAG_NAME, "a")
        if not apn_links:
            continue
        parcel_id = parse_qs(urlparse(apn_links[0].get_attribute("href")).query).get("q", [""])[0]
        owner_name = cells[1].text.strip()
        address = cells[2].text.strip()
        if parcel_id and owner_name:
            rows.append((parcel_id, owner_name, address))
    return rows


def parse_address(full_address):
    """Maricopa's address column already comes as 'STREET, CITY, ZIP' -
    parse it directly instead of burning a Google search on every match.
    Returns None when that shape doesn't hold, so the caller can fall back
    to geocode.geocode_address()."""
    parts = [p.strip() for p in (full_address or "").split(",")]
    if len(parts) >= 3:
        zip_match = ZIP_RE.search(parts[2])
        if zip_match:
            return parts[0], parts[1], zip_match.group(1)
    return None


def search_owner(driver, query):
    """Every real-property result row for `query`, across up to MAX_PAGES
    of this dedicated real-property search results page."""
    all_rows = []
    total = None
    page = 1
    while page <= MAX_PAGES:
        driver.get(SEARCH_URL.format(query=quote_plus(query), page=page))
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "table tbody tr td")
        )
        if total is None:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            match = re.search(r"Showing[^\n]*results", body_text)
            total = parse_total_results(match.group(0) if match else "")
        all_rows.extend(parse_rows(driver))
        if not total or page * RESULTS_PER_PAGE >= total:
            break
        page += 1
    return all_rows


if __name__ == "__main__":
    pipeline_db.resolve_mode(sys.argv)  # not used here, but keeps argv[1] consistent with the rest of the pipeline

    names_with_dates = pipeline_db.get_obituary_names_with_dates()
    pipeline_db.clear_matches()

    print("Names to search:")
    for name, posted_date in names_with_dates:
        print(f"{name} ({posted_date})" if posted_date else name)

    # Fresh browser per name, matching SLK$.py's and pricer.py's own
    # convention here - simpler than reusing one session across every
    # search, and a crashed/stuck browser only costs one name instead of
    # the whole run.
    for name, posted_date in names_with_dates:
        driver = create_driver()
        try:
            last_name = extract_last_name(name)
            first_name = extract_first_name(name)
            if not last_name or not first_name:
                print(f"Skipping '{name}' - couldn't parse a first/last name.")
                continue

            query = f"{first_name} {last_name}"
            print(f"Searching for: {query}")
            rows = search_owner(driver, query)

            if not rows:
                print(f"No results found for {name}. Moving on.")
                continue

            for parcel_id, owner_name, address in rows:
                owner_words = owner_name.split()
                if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
                    continue

                parsed = parse_address(address)
                if parsed:
                    street, city, zip_code = parsed
                    state = geocode.STATE_ABBR
                else:
                    street = address.split(",")[0].strip()
                    city, state, zip_code = geocode.geocode_address(driver, street)

                print(f"Match found for {name}: owner={owner_name}, parcel={parcel_id}, address={street}")
                pipeline_db.add_match(name, owner_name, parcel_id, street, city, state, zip_code, posted_date)

        except Exception as e:
            print(f"Error during search for '{name}': {e}")
        finally:
            driver.quit()

    print("Done.")
