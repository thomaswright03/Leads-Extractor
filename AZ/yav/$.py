import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote_plus

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db
import geocode

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Yavapai County's own Assessor site doesn't expose an owner-name search
# itself - the county Treasurer's public "Parcel Search" tool does
# (confirmed live via yavapaiaz.gov -> Assessor's Office -> "Property Tax
# inquiry" -> "Search By Owner"), and it's also where the mailing address
# on file lives (tax bills go to whatever address is on record here, same
# reasoning as every other county's assessor site elsewhere in this
# codebase). This is a completely different platform from every AZ county
# built so far - a plain ASP.NET MVC + Kendo UI grid app ("Pinal & Yavapai
# IGA"), not Tyler EagleWeb - and unlike EagleWeb it's fully unauthenticated
# (no guest-login step at all) and stateless: every page takes its own
# parcel number as a plain query param, confirmed live, so there's no
# session/cookie dependency between requests the way EagleWeb has.
SEARCH_URL_TEMPLATE = "https://taxinquiry.yavapaiaz.gov/Main/ParcelSearch?fullName={last_name}&Grid-page={page}&Grid-pageSize=50"
TAX_SUMMARY_URL = "https://taxinquiry.yavapaiaz.gov/Parcel/TaxSummary?parcelnumber={parcel_id}"

ZIP_RE = re.compile(r"\b(\d{5})\b")

# This site's own numbered pager tops out at 50 items/page (confirmed
# live) - a common surname like "Smith" has been seen live at 2121 total
# matches, so MAX_PAGES stays a modest safety cap, same reasoning as every
# other county here: not worth dozens of page loads chasing one surname
# when the first-name filter still has to run per row regardless.
MAX_PAGES = 6


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
    return uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)


GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _strip_suffix_clause(full_name):
    """Obituary listing names occasionally carry a trailing rank/title
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") - a
    comma-separated generational suffix (e.g. "Robert Smith, Jr.") is kept,
    since that's part of the actual name."""
    name, sep, suffix = full_name.partition(",")
    if sep and suffix.strip().rstrip(".").lower() not in GENERATIONAL_SUFFIXES:
        return name.strip()
    return full_name.strip()


def extract_last_name(full_name):
    name_parts = _strip_suffix_clause(full_name).split()
    while name_parts and name_parts[-1].lower().rstrip(".") in GENERATIONAL_SUFFIXES:
        name_parts = name_parts[:-1]
    name_parts = re.sub(r"\(.*?\)", "", " ".join(name_parts)).split()
    # A kept comma-separated generational suffix (e.g. "Robert Smith, Jr.")
    # leaves its comma stuck to the word before it once the suffix word
    # itself is stripped off above ("Smith," not "Smith") - strip it here so
    # the search field doesn't get a trailing comma that would silently
    # match nothing.
    return name_parts[-1].rstrip(",") if name_parts else ""


def extract_first_name(full_name):
    name_parts = re.sub(r"\(.*?\)", "", _strip_suffix_clause(full_name)).split()
    return name_parts[0] if name_parts else ""


def parse_result_rows(driver):
    """Parcel # + owner name straight off the results grid - cheap, no
    extra page load. This site's "contains" search matches business/lease
    records too (e.g. "DFS-SPV LP (LESSOR)" alongside a genuine "SMITH ..."
    lessee name - confirmed live), and unlike Tyler EagleWeb's account-
    number prefixes ("R"/"M"/"C"), there's no clean field here to filter
    those out by. The first-name match this feeds into is what actually
    screens the noise out instead - same fallback $.md's guidance describes
    for a site with no such filter available."""
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "table[role='grid'] tbody tr"):
        try:
            link = tr.find_element(By.CSS_SELECTOR, "a[href*='parcelnumber=']")
            parcel_display = link.text.strip()
            cells = tr.find_elements(By.CSS_SELECTOR, "td[role='gridcell']")
            # Cell 1 is Owner Name - co-owners render as two <br>-separated
            # lines in the same cell (e.g. "NAISMITH WILLIAM ANDY OR" /
            # "NAISMITH DIANA LUCILLE") - .text preserves both as separate
            # lines, and the first-name check downstream just splits on
            # whitespace, so both co-owners' names are checked either way.
            owner_name = cells[1].text.strip() if len(cells) > 1 else ""
            if parcel_display and owner_name:
                rows.append((parcel_display, owner_name))
        except Exception:
            continue
    return rows


def collect_candidates(driver, last_name):
    """All (parcel_display, owner_name) rows across every page of results.
    This platform's own pagination is a plain "Grid-page=<N>" query param
    (confirmed live) - pages are requested directly rather than needing to
    scrape a "next page" link, stopping as soon as a page comes back empty."""
    seen = {}
    for page in range(1, MAX_PAGES + 1):
        driver.get(SEARCH_URL_TEMPLATE.format(last_name=quote_plus(last_name), page=page))
        WebDriverWait(driver, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, "table[role='grid']"))
        rows = parse_result_rows(driver)
        if not rows:
            break
        for parcel_display, owner_name in rows:
            seen[parcel_display] = owner_name
    return list(seen.items())


def parse_parcel_detail(driver, parcel_display):
    """Owner name and mailing address off this parcel's Tax Summary page -
    a plain <fieldset class="addressblock"> whose lines (name(s), street,
    city/state/zip) are <br>-separated under one legend link, confirmed
    live. The number of name lines varies (1 owner vs 2 co-owners), so this
    takes the last two lines as street/city-state-zip and everything before
    that as the owner name(s), rather than assuming a fixed line count."""
    digits_only = "".join(ch for ch in parcel_display if ch.isalnum())
    driver.get(TAX_SUMMARY_URL.format(parcel_id=digits_only))
    WebDriverWait(driver, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, "fieldset.addressblock"))

    block = driver.find_element(By.CSS_SELECTOR, "fieldset.addressblock")
    lines = [line.strip() for line in block.text.split("\n") if line.strip()]
    # First line is the fieldset's own legend link text, not part of the
    # address itself.
    lines = [line for line in lines if line.lower() != "current owner name & mailing address"]

    if len(lines) < 2:
        return "", "", "", "", ""

    owner_name = " ".join(lines[:-2])
    street = lines[-2]
    city_state_zip = lines[-1]
    city, state, zip_code = "", "", ""
    zip_match = ZIP_RE.search(city_state_zip)
    zip_code = zip_match.group(1) if zip_match else ""
    before_zip = city_state_zip[: zip_match.start()].strip().rstrip(",") if zip_match else city_state_zip
    if "," in before_zip:
        city, state = (p.strip() for p in before_zip.rsplit(",", 1))
    else:
        parts = before_zip.split()
        if len(parts) >= 2:
            state = parts[-1]
            city = " ".join(parts[:-1])

    return owner_name, street, city, state, zip_code


if __name__ == "__main__":
    pipeline_db.resolve_mode(sys.argv)  # not used here, but keeps argv[1] consistent with the rest of the pipeline

    names_with_dates = pipeline_db.get_obituary_names_with_dates()
    pipeline_db.clear_matches()

    print("Names to search:")
    for name, posted_date in names_with_dates:
        print(f"{name} ({posted_date})" if posted_date else name)

    # Fresh browser per name, matching every other county's $.py convention
    # here except Gila/Yuma's - this site has shown no sign of the AWS WAF
    # bot-detection confirmed live on those two (no guest-login wall, no
    # CAPTCHA seen during development), so there's no reason to depart from
    # the simpler default: a crashed/stuck browser only costs one name
    # instead of the whole run.
    for name, posted_date in names_with_dates:
        driver = create_driver()
        try:
            last_name = extract_last_name(name)
            first_name = extract_first_name(name)
            if not last_name or not first_name:
                print(f"Skipping '{name}' - couldn't parse a first/last name.")
                continue

            print(f"Searching for last name: {last_name}")
            time.sleep(random.uniform(0.5, 1.5))
            candidates = collect_candidates(driver, last_name)

            if not candidates:
                print(f"No results found for {name}. Moving on.")
                continue

            for parcel_display, owner_name in candidates:
                owner_words = owner_name.split()
                if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
                    continue

                try:
                    full_owner_name, street, city, state, zip_code = parse_parcel_detail(driver, parcel_display)
                except Exception as e:
                    print(f"  Couldn't load parcel detail for {parcel_display}: {e}")
                    continue

                if not street:
                    print(f"  {parcel_display} ({owner_name}) has no mailing address on file - skipping.")
                    continue

                if not city or not state or not zip_code:
                    city, state, zip_code = geocode.geocode_address(driver, street, city or None)

                print(f"Match found for {name}: owner={full_owner_name or owner_name}, parcel={parcel_display}, address={street}")
                pipeline_db.add_match(
                    name, full_owner_name or owner_name, parcel_display,
                    street, city, state, zip_code, posted_date,
                )

        except Exception as e:
            print(f"Error during search for '{name}': {e}")
        finally:
            driver.quit()

    print("Done.")
