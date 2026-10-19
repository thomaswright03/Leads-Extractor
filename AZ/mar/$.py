import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote_plus, urlparse, parse_qs

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
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
    # undetected_chromedriver, not plain webdriver.Chrome() - this driver's
    # geocode_address() fallback hits google.com directly, and plain
    # Selenium gets CAPTCHA-walled by Google almost immediately (same
    # reasoning UT/uta's equivalent script already follows).
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


GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


LEADING_TITLES = {"the", "rev", "reverend", "fr", "father", "pastor", "dr", "mr", "mrs", "ms", "sister", "sr"}


def _drop_nicknames_and_titles(full_name):
    """Quoted nicknames and leading titles aren't part of the name an
    assessor files a deed under. Seen live 2026-10-19: 'Geraldine "Gerry" ,
    "Granny" Fuller' searched for 'Geraldine "Gerry"', and 'Fr. Frank Weil'
    or 'The Rev. ...' used the title as the first name."""
    name = re.sub(r'\s*,?\s*["\u201c\u201d][^"\u201c\u201d]*["\u201c\u201d]\s*,?\s*', " ", full_name).strip(" ,")
    parts = name.split()
    while len(parts) > 2 and parts[0].lower().rstrip(".") in LEADING_TITLES:
        parts = parts[1:]
    return " ".join(parts)


def _strip_suffix_clause(full_name):
    """Obituary listing names occasionally carry a trailing rank/title
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") - confirmed
    live to corrupt last-name extraction (grabbing "Col/R" instead of
    "Kitchin"), which degrades the search into matching almost any owner
    with that first name. A comma-separated generational suffix (e.g.
    "Robert Smith, Jr.") is kept, since that's part of the actual name."""
    full_name = _drop_nicknames_and_titles(full_name)
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


def _load_results_page(driver, url):
    """Load one results page and wait for its table. The site sits behind a
    Cloudflare "Just a moment..." check that some page loads hit (6 of ~60
    searches on 2026-10-19); it usually clears by itself within seconds, so
    give it longer and one reload rather than losing the name."""
    has_rows = lambda d: d.find_elements(By.CSS_SELECTOR, "table tbody tr td")
    for attempt in (1, 2):
        driver.get(url)
        try:
            WebDriverWait(driver, 15).until(has_rows)
            return
        except TimeoutException:
            if driver.title != "Just a moment...":
                raise
        print("  Waiting on Maricopa's Cloudflare check...")
        try:
            WebDriverWait(driver, 30).until(has_rows)
            return
        except TimeoutException:
            if attempt == 2:
                raise
            time.sleep(5)


def search_owner(driver, query):
    """Every real-property result row for `query`, across up to MAX_PAGES
    of this dedicated real-property search results page."""
    all_rows = []
    total = None
    page = 1
    while page <= MAX_PAGES:
        _load_results_page(driver, SEARCH_URL.format(query=quote_plus(query), page=page))
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
            print(f"Error during search for '{name}': {type(e).__name__}: {e}")
            # A bare TimeoutException has an empty message - say what page
            # the browser was actually stuck on.
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text.strip().replace("\n", " | ")
                print(f"  Stuck on: title={driver.title!r} url={driver.current_url} body={body_text[:400]!r}")
            except Exception:
                pass
        finally:
            driver.quit()

    print("Done.")
