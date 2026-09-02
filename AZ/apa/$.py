import re
import shutil
import subprocess
import sys
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db
import geocode

# Apache County's assessor site runs on Tyler Technologies' EagleWeb -
# confirmed live via apachecountyaz.gov -> "EagleWeb Property Records
# Database". Owner-name search is a plain last-name text field (no fuzzy
# address matching like Maricopa's single search box), so this searches
# last-name-only, straight per $.md's documented heuristic.
SEARCH_URL = "https://eagleassessor.co.apache.az.us/assessor/taxweb/search.jsp"
ACCOUNT_URL = "https://eagleassessor.co.apache.az.us/assessor/taxweb/account.jsp?accountNum={account_num}&guest=true"

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


# Confirmed live in CI: consecutive per-name browser launches went
# completely silent (no match, no "no results", no error at all) right
# after an earlier name's search crashed - stdout's default block buffering
# was delaying/losing whatever real error was happening, and a fresh
# uc.Chrome() launch right after a crash can fail transiently while the
# previous session's resources are still being torn down. Unbuffered output
# plus a retry on driver creation turns that silence into a real, visible
# error instead.
sys.stdout.reconfigure(line_buffering=True)


def new_driver_with_retries(max_attempts=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return create_driver()
        except Exception as e:
            last_error = e
            print(f"  create_driver() failed (attempt {attempt}/{max_attempts}): {type(e).__name__}: {e}")
            time.sleep(2)
    raise last_error


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
    return name_parts[-1] if name_parts else ""


def extract_first_name(full_name):
    name_parts = re.sub(r"\(.*?\)", "", _strip_suffix_clause(full_name)).split()
    return name_parts[0] if name_parts else ""


def search_last_name(driver, last_name):
    # ?guest=true is what actually establishes a usable guest session on
    # this site - hitting search.jsp directly without it bounces to a login
    # page instead (confirmed live). A fresh driver has no session yet, so
    # this needs to run every time, not just once.
    driver.get(f"{SEARCH_URL}?guest=true")
    WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.ID, "OwnerIDSearchString"))
    field = driver.find_element(By.ID, "OwnerIDSearchString")
    field.clear()
    field.send_keys(last_name)
    driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
    WebDriverWait(driver, 15).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "tr[class^='tableRow']")
        or "No results found for query" in d.page_source
    )
    # EagleWeb has been seen putting every result on one page even for a
    # fairly common local surname (33 rows for "Begay", live-confirmed) -
    # this is a small rural county, so multi-page result sets are the
    # exception, not the rule. Flag it loudly if it ever happens rather
    # than silently only searching page 1.
    body_text = driver.find_element(By.TAG_NAME, "body").text
    pages_match = re.search(r"on (\d+) pages?", body_text)
    if pages_match and int(pages_match.group(1)) > 1:
        print(f"  WARNING: '{last_name}' search spans {pages_match.group(1)} pages - only page 1 is searched.")


def parse_result_rows(driver):
    """Account number + owner name straight off the results list - cheap,
    no extra page load. Used to filter down to first-name matches before
    spending a page load on each candidate's account detail page (see
    parse_account_detail). Real-property accounts only (account numbers
    starting with "R") - "M" (mobile home) and "P" (personal property)
    accounts aren't the kind of real-estate probate lead this business
    targets, and are common enough in this county (manufactured housing on
    tribal/trust land, titled separately from the land) to be worth
    filtering out here rather than downstream."""
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "tr[class^='tableRow']"):
        try:
            account_num = tr.find_element(By.CSS_SELECTOR, "td.clickable a").text.strip()
            if not account_num.startswith("R"):
                continue
            # Scoping to the nested summary table itself (not `tr`) matters:
            # every cell in it also carries class="clickable", same as the
            # row's own outer cells - searching from `tr` for "td.clickable"
            # or "table td" matches both sets at once (confirmed live: 8
            # results instead of the nested table's real 5), silently
            # shifting every index over and grabbing the wrong cell's text.
            summary_table = tr.find_element(By.CSS_SELECTOR, "a table")
            cells = summary_table.find_elements(By.CSS_SELECTOR, "td")
            owner_name = cells[1].text.strip() if len(cells) > 1 else ""
            if account_num and owner_name:
                rows.append((account_num, owner_name))
        except Exception:
            continue
    return rows


def _labeled_text(driver, tag, label):
    els = driver.find_elements(By.XPATH, f"//{tag}[normalize-space(text())='{label}']/parent::td")
    return els[0].text.strip() if els else ""


def parse_account_detail(driver, account_num):
    """Owner name and mailing address off this account's detail page - the
    results list itself doesn't reliably carry a usable address for this
    county (many rural real-property parcels have no situs address at all,
    e.g. "37 5053" with no street name, confirmed live), so the owner's
    mailing address on file is the real, consistently-populated source."""
    driver.get(ACCOUNT_URL.format(account_num=account_num))
    WebDriverWait(driver, 15).until(
        lambda d: d.find_elements(By.XPATH, "//b[normalize-space(text())='Owner Name']")
        or "unable to complete your request" in d.page_source.lower()
    )
    if "unable to complete your request" in driver.page_source.lower():
        raise ValueError(f"Account {account_num} didn't load.")

    owner_name = _labeled_text(driver, "b", "Owner Name").replace("Owner Name", "", 1).strip()

    address_text = _labeled_text(driver, "b", "Owner Address").replace("Owner Address", "", 1).strip()
    lines = [line.strip() for line in address_text.split("\n") if line.strip()]
    street = lines[0] if lines else ""
    city, state, zip_code = "", "", ""
    if len(lines) > 1:
        city_state_zip = lines[1]
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
    # here - simpler than reusing one session across every search, and a
    # crashed/stuck browser only costs one name instead of the whole run.
    for name, posted_date in names_with_dates:
        try:
            driver = new_driver_with_retries()
        except Exception as e:
            print(f"Error during search for '{name}': couldn't start a browser after retries: {type(e).__name__}: {e}")
            continue

        try:
            last_name = extract_last_name(name)
            first_name = extract_first_name(name)
            if not last_name or not first_name:
                print(f"Skipping '{name}' - couldn't parse a first/last name.")
                continue

            print(f"Searching for last name: {last_name}")
            search_last_name(driver, last_name)
            rows = parse_result_rows(driver)

            if not rows:
                print(f"No results found for {name}. Moving on.")
                continue

            for account_num, owner_name in rows:
                owner_words = owner_name.split()
                if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
                    continue

                try:
                    full_owner_name, street, city, state, zip_code = parse_account_detail(driver, account_num)
                except Exception as e:
                    print(f"  Couldn't load account detail for {account_num}: {e}")
                    continue

                if not street:
                    print(f"  {account_num} ({owner_name}) has no mailing address on file - skipping.")
                    continue

                if not city or not state or not zip_code:
                    city, state, zip_code = geocode.geocode_address(driver, street, city or None)

                print(f"Match found for {name}: owner={full_owner_name or owner_name}, parcel={account_num}, address={street}")
                pipeline_db.add_match(
                    name, full_owner_name or owner_name, account_num,
                    street, city, state, zip_code, posted_date,
                )

        except Exception as e:
            print(f"Error during search for '{name}': {type(e).__name__}: {e}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    print("Done.")
