import re
from urllib.parse import quote_plus

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

STATE_ABBR = 'AZ'
# e.g. "Summit County, UT" - also doubles as the stand-in "city, state" for
# the Google query below when the real city isn't known.
COUNTY_QUERY_SUFFIX = 'Gila County, AZ'
COUNTY_NAME_FALLBACK = COUNTY_QUERY_SUFFIX.rsplit(",", 1)[0].strip()

ZIP_PATTERN = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def geocode_address(driver, street_address, city=None):
    """Look up the zip code for street_address by searching the full
    address on Google and reading whatever 5-digit zip shows up in the
    results. This needs address + city + state to resolve reliably - the
    assessor site doesn't always give a real city, so the county's own name
    stands in for it when city isn't known (Google can still resolve a real
    street address correctly with just county + state as context).

    driver is an already-open Selenium browser - reuse whichever one is
    already mid-search for this name rather than spinning up a dedicated
    one just for this lookup.
    """
    state = STATE_ABBR
    if not street_address:
        return "", state, ""

    city = city or COUNTY_NAME_FALLBACK
    query = f"{street_address}, {city}, {state}"

    driver.get(f"https://www.google.com/search?q={quote_plus(query)}")
    try:
        WebDriverWait(driver, 10).until(
            lambda d: ZIP_PATTERN.search(d.find_element(By.TAG_NAME, "body").text)
        )
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        text = ""

    match = ZIP_PATTERN.search(text)
    zip_code = match.group(0)[:5] if match else ""
    return city, state, zip_code
