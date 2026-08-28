import re
import shutil
import subprocess
import sys

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db
import geocode

# Greenlee County's assessor site runs on Harris Govern's RealWare DataMart
# property inquiry tool (confirmed live via greenlee.az.gov -> Assessor page
# -> parcelsearch.greenlee.az.gov). Owner-name search is a plain "contains"
# text field, not last-name-only matching like other counties here (it also
# matches the search text mid-name, e.g. "Smith" matches "NESSMITH" too) -
# that's fine, the first-name filter below still only accepts real matches.
INQUIRY_URL = "https://parcelsearch.greenlee.az.gov/Production/RealWareDataMartPropertyInquiryWebsite/Inquiry.aspx"

MAX_PAGES = 5  # safety cap on "More Results" clicks - 50 rows is generous for one surname

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


def _is_single_result_page(driver):
    # A search matching exactly one property skips the results list and
    # lands straight on that property's own detail page (confirmed live,
    # both for an owner-name search and an account-number search) - this
    # heading only ever appears on that detail-page layout.
    return bool(driver.find_elements(By.XPATH, "//td[normalize-space(text())='Owner Name & Address']"))


def _no_results(driver):
    return "did not match any parcels" in driver.page_source


def _clear_search_form(driver):
    # This site remembers every search field server-side across requests in
    # the same session, not just within one page instance - even a fresh
    # GET of Inquiry.aspx comes back with the previous search's fields
    # already filled in (confirmed live). Left alone, a later search is
    # silently ANDed with a now-irrelevant earlier field (e.g. an account
    # number from a prior lookup) and returns zero results instead of the
    # new criteria's real matches. The page's own "Clear" button fires a
    # real __doPostBack despite looking like a plain client-side reset
    # (confirmed live), which reloads the whole page and races with
    # whatever runs right after it - clearing the input elements directly
    # avoids a postback entirely, and is all that's actually needed since
    # a plain HTML form submits whatever value the input currently holds.
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctNumberTxt").clear()
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctOwnerTxt").clear()


def search_owner(driver, last_name):
    driver.get(INQUIRY_URL)
    WebDriverWait(driver, 30).until(lambda d: d.find_elements(By.ID, "ctl00_ContentPlaceHolder1_AcctOwnerTxt"))
    _clear_search_form(driver)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctOwnerTxt").send_keys(last_name)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_SearchBtn").click()
    WebDriverWait(driver, 30).until(
        lambda d: _no_results(d)
        or _is_single_result_page(d)
        or d.find_elements(By.CSS_SELECTOR, "a[id$='_AccountNoLnkInternal']")
    )


def search_account_number(driver, account_num):
    # An exact account-number search always matches at most one record, so
    # it always auto-redirects straight to that account's own detail page
    # (confirmed live) - this is how a specific candidate from a multi-row
    # owner-name search gets its full detail without reusing that search's
    # own row links, which are scoped to the session that generated them
    # and go stale as soon as a *different* search runs (confirmed live).
    driver.get(INQUIRY_URL)
    WebDriverWait(driver, 30).until(lambda d: d.find_elements(By.ID, "ctl00_ContentPlaceHolder1_AcctNumberTxt"))
    _clear_search_form(driver)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_AcctNumberTxt").send_keys(account_num)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_SearchBtn").click()
    WebDriverWait(driver, 30).until(lambda d: _no_results(d) or _is_single_result_page(d))


def parse_account_detail(driver):
    """Owner name, account number, and mailing address off whichever
    ParcelDetail-shaped page is currently loaded - works the same whether
    reached via a list-row click or a single-result auto-redirect."""
    account_num = (
        driver.find_element(By.XPATH, "//strong[normalize-space(text())='Account Number:']/parent::td")
        .text.replace("Account Number:", "", 1).strip()
    )
    owner_cell = driver.find_element(
        By.XPATH, "//td[normalize-space(text())='Owner Name & Address']/parent::tr/following-sibling::tr[1]/td[3]"
    )
    lines = [line.strip() for line in owner_cell.text.split("\n") if line.strip()]
    # lines[0] is the "Primary Owner" label, the last line is an ownership
    # percentage (e.g. "100.00%") - neither is part of the name/address.
    owner_name = lines[1] if len(lines) > 1 else ""
    street = lines[2] if len(lines) > 2 else ""
    city, state, zip_code = "", "", ""
    if len(lines) > 3:
        city_state_zip = lines[3]
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
    return account_num, owner_name, street, city, state, zip_code


def parse_result_rows(driver):
    """Account number + owner name straight off the results list - cheap,
    no extra page load. Used to filter down to first-name matches before
    spending a page load on each candidate's own detail page. Real-property
    accounts only (account numbers starting with "R") - "M" (mobile home)
    and "P" (personal property) accounts aren't the kind of real-estate
    probate lead this business targets."""
    rows = []
    for link in driver.find_elements(By.CSS_SELECTOR, "a[id$='_AccountNoLnkInternal']"):
        account_num = link.text.strip()
        if not account_num.startswith("R"):
            continue
        tr = link.find_element(By.XPATH, "./ancestor::tr[1]")
        cells = tr.find_elements(By.TAG_NAME, "td")
        owner_name = cells[2].text.strip() if len(cells) > 2 else ""
        if account_num and owner_name:
            rows.append((account_num, owner_name))
    return rows


def collect_candidates(driver):
    """All real-property (account_num, owner_name) rows across every page
    of the current search's results, clicking "More Results" as needed."""
    if _no_results(driver):
        return []
    if _is_single_result_page(driver):
        account_num, owner_name, *_ = parse_account_detail(driver)
        return [(account_num, owner_name)] if account_num.startswith("R") else []

    seen_accounts = {}
    for account_num, owner_name in parse_result_rows(driver):
        seen_accounts[account_num] = owner_name

    for _ in range(MAX_PAGES - 1):
        next_links = driver.find_elements(By.ID, "ctl00_ContentPlaceHolder1_linkNext")
        # RealWare keeps rendering the "More Results" link even on the last
        # page - it just goes href-less (and non-displayed) instead of
        # disappearing (confirmed live: a `javascript:__doPostBack(...)`
        # href on every page except the last, where it's None) - clicking
        # it anyway is a no-op that just re-renders the same last page.
        if not next_links or not next_links[0].get_attribute("href"):
            break
        # A plain .click() has been seen raising ElementNotInteractable on
        # this link (confirmed live) even though it's genuinely there and
        # working - a JS-dispatched click sidesteps whatever visibility/
        # geometry check Selenium's native click was failing on.
        driver.execute_script("arguments[0].click();", next_links[0])
        # A "next page" with exactly one row left doesn't render as a
        # one-row list - it auto-redirects straight to that record's own
        # detail page, the same single-result shape a fresh search can
        # land on (confirmed live: reproduced 100% of the time on a real
        # search where the last page held exactly one match - explains a
        # run that otherwise looked like an unrelated environment timeout,
        # since waiting for list-only markup here never resolved). Both
        # possible next shapes have to be in the wait, not just the list.
        WebDriverWait(driver, 30).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "a[id$='_AccountNoLnkInternal']")
            or _is_single_result_page(d)
        )
        if _is_single_result_page(driver):
            account_num, owner_name, *_ = parse_account_detail(driver)
            if account_num.startswith("R"):
                seen_accounts[account_num] = owner_name
            break
        for account_num, owner_name in parse_result_rows(driver):
            seen_accounts[account_num] = owner_name

    return list(seen_accounts.items())


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
        driver = create_driver()
        try:
            last_name = extract_last_name(name)
            first_name = extract_first_name(name)
            if not last_name or not first_name:
                print(f"Skipping '{name}' - couldn't parse a first/last name.")
                continue

            print(f"Searching for last name: {last_name}")
            search_owner(driver, last_name)
            candidates = collect_candidates(driver)

            if not candidates:
                print(f"No results found for {name}. Moving on.")
                continue

            for account_num, owner_name in candidates:
                owner_words = owner_name.split()
                if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
                    continue

                try:
                    # A single-result page is already the detail page - no
                    # separate search-by-account-number round trip needed.
                    if _is_single_result_page(driver):
                        _, full_owner_name, street, city, state, zip_code = parse_account_detail(driver)
                    else:
                        search_account_number(driver, account_num)
                        _, full_owner_name, street, city, state, zip_code = parse_account_detail(driver)
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
            print(f"Error during search for '{name}': {e}")
        finally:
            driver.quit()

    print("Done.")
