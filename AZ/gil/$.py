import random
import re
import shutil
import subprocess
import sys
import time

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
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

# Gila County's assessor site runs on the exact same Tyler Technologies
# EagleWeb/TaxWeb platform as Apache County's (confirmed live: identical
# field ids - OwnerIDSearchString/OwnerIDSearchType - identical result-row
# markup - tr.tableRow1/2, td.clickable, a nested "a table" summary cell -
# and identical account.jsp detail-page layout, "Owner Name"/"Owner Address"
# bold-labeled cells). Only the domain and the login step differ.
SEARCH_URL = "https://gilacountyaz-assessor.tylerhost.net/assessor/taxweb/search.jsp"
ACCOUNT_URL = "https://gilacountyaz-assessor.tylerhost.net/assessor/taxweb/account.jsp?accountNum={account_num}&guest=true"

# Apache's identical deployment of this platform accepts a bare
# "?guest=true" query param straight on search.jsp and is immediately
# usable (confirmed live there). Gila's own login page still bounced a
# fresh, guest=true-tagged request back to itself (confirmed live) - what
# actually works here is submitting the "Public Users" form on that login
# page, which POSTs guest=true in the request body instead of the query
# string. ensure_logged_in() tries the cheap shortcut first and falls back
# to clicking that form so this keeps working if a future Gila deployment
# ever starts honoring the query-param shortcut too.
GUEST_LOGIN_XPATH = "//form[.//input[@name='guest']]//input[@type='submit']"

ZIP_RE = re.compile(r"\b(\d{5})\b")

# This is a much bigger county than Apache - a common surname (e.g. "Smith")
# has been seen live spanning 3 result pages, unlike Apache's small rural
# rolls where everything fits on one page - so unlike apa/$.py, this
# actually has to page through results rather than just warning about page 2+.
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
    # geocode_address() fallback (see below) hits google.com directly, and
    # plain Selenium gets CAPTCHA-walled by Google almost immediately
    # (same reasoning UT/uta's equivalent script already follows for its
    # own Google-touching driver). This site's own confirmed-live AWS WAF
    # bot-detection is a second, independent reason.
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


class CaptchaBlockedError(Exception):
    """Raised when this site's AWS WAF bot-detection throws up its
    "Human Verification" CAPTCHA wall. Confirmed live during development:
    a burst of rapid, back-to-back automated requests to this exact site
    triggered it more than once. Solving/bypassing a CAPTCHA is off the
    table on principle, so this stops the whole run rather than trying to
    click through it - once one request gets walled, every other request
    from the same session/IP is likely to be too."""


def _check_not_walled(driver):
    # Confirmed live 2026-10-19: from GitHub Actions' (Azure) runners the
    # search page itself now answers a flat "403 Forbidden" - the site
    # blocks those IPs outright. Stop the run with a clear failure instead
    # of timing out on every name and exiting 0 as if all was well.
    if driver.title == "403 Forbidden":
        raise CaptchaBlockedError(
            "Gila County's assessor site answered 403 Forbidden - it is "
            "blocking this machine's IP address (GitHub Actions runners are "
            "blocked as of 2026-10-19). Run this county from another network."
        )
    if driver.title == "Human Verification" or "let's confirm you are human" in driver.page_source.lower():
        raise CaptchaBlockedError(
            "Gila County's assessor site put up its AWS WAF 'Human "
            "Verification' CAPTCHA wall. This pipeline will not attempt to "
            "solve it. Stop and retry later with more spacing between "
            "requests, or from a different network."
        )


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
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") - a
    comma-separated generational suffix (e.g. "Robert Smith, Jr.") is kept,
    since that's part of the actual name."""
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
    # A kept comma-separated generational suffix (e.g. "Robert Smith, Jr.")
    # leaves its comma stuck to the word before it once the suffix word
    # itself is stripped off above ("Smith," not "Smith") - strip it here so
    # a plain-text search field doesn't get a trailing comma that would
    # silently match nothing.
    return name_parts[-1].rstrip(",") if name_parts else ""


def extract_first_name(full_name):
    name_parts = re.sub(r"\(.*?\)", "", _strip_suffix_clause(full_name)).split()
    return name_parts[0] if name_parts else ""


def ensure_logged_in(driver):
    driver.get(f"{SEARCH_URL}?guest=true")
    _check_not_walled(driver)
    if driver.find_elements(By.ID, "OwnerIDSearchString"):
        return
    guest_buttons = driver.find_elements(By.XPATH, GUEST_LOGIN_XPATH)
    if guest_buttons:
        guest_buttons[0].click()
    WebDriverWait(driver, 20).until(lambda d: d.find_elements(By.ID, "OwnerIDSearchString"))
    _check_not_walled(driver)


def search_last_name(driver, last_name):
    ensure_logged_in(driver)
    field = driver.find_element(By.ID, "OwnerIDSearchString")
    field.clear()
    field.send_keys(last_name)
    # Exactly one <form> on this page (confirmed live), so exactly one
    # submit control regardless of tag - <input type=submit> vs <button
    # type=submit> varies by exact deployment, so accept either.
    driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']")[0].click()
    WebDriverWait(driver, 20).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "tr[class^='tableRow']")
        or "Showing 0" in d.page_source
        or d.title == "Human Verification"
    )
    _check_not_walled(driver)


def parse_result_rows(driver):
    """Account number + owner name straight off the results list - cheap,
    no extra page load. Used to filter down to first-name matches before
    spending a page load on each candidate's account detail page. Real-
    property accounts only (account numbers starting with "R") - "M"
    (mobile home) and "C" (business/commercial) accounts aren't the kind of
    real-estate probate lead this business targets (confirmed live: a plain
    "Smith" search returns plenty of both alongside the "R" accounts)."""
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "tr[class^='tableRow']"):
        try:
            account_num = tr.find_element(By.CSS_SELECTOR, "td.clickable a").text.strip()
            if not account_num.startswith("R"):
                continue
            # Scoping to the nested summary table itself (not `tr`) matters:
            # every cell in it also carries class="clickable", same as the
            # row's own outer cells - searching from `tr` for "td.clickable"
            # would double up and shift indexes (same gotcha documented in
            # apa/$.py for this identical platform).
            summary_table = tr.find_element(By.CSS_SELECTOR, "a table")
            cells = summary_table.find_elements(By.CSS_SELECTOR, "td")
            owner_name = cells[1].text.strip() if len(cells) > 1 else ""
            if account_num and owner_name:
                rows.append((account_num, owner_name))
        except Exception:
            continue
    return rows


START_RE = re.compile(r"[?&]start=(\d+)")


def _current_start(driver):
    match = START_RE.search(driver.current_url)
    return int(match.group(1)) if match else 0


def _next_page_url(driver):
    """The smallest "start=" offset link on the current page that's greater
    than the current page's own offset - i.e. the real "next page" link.
    Confirmed live on Yuma County's identical deployment of this platform:
    its numbered pager (e.g. "1 2 3 ... 34 Next->") is plain
    "results.jsp?start=<N>" links - development hit this county's own
    CAPTCHA wall before a genuine page-2+ link could be inspected directly
    here, but it's the same platform, so this reuses Yuma's confirmed
    mechanism instead of a page-size guess."""
    current = _current_start(driver)
    candidates = []
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='start=']"):
        href = a.get_attribute("href") or ""
        match = START_RE.search(href)
        if match:
            start = int(match.group(1))
            if start > current:
                candidates.append((start, href))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def collect_candidates(driver):
    """All real-property (account_num, owner_name) rows across every page
    of the current search's results, following the page's own real "next
    page" links rather than guessing offsets."""
    if "Showing 0" in driver.find_element(By.TAG_NAME, "body").text:
        return []

    seen = {}
    for account_num, owner_name in parse_result_rows(driver):
        seen[account_num] = owner_name

    for _ in range(MAX_PAGES - 1):
        next_url = _next_page_url(driver)
        if not next_url:
            break
        driver.get(next_url)
        WebDriverWait(driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "tr[class^='tableRow']")
            or "Showing 0" in d.page_source
            or d.title == "Human Verification"
        )
        _check_not_walled(driver)
        for account_num, owner_name in parse_result_rows(driver):
            seen[account_num] = owner_name

    return list(seen.items())


def _labeled_text(driver, tag, label):
    els = driver.find_elements(By.XPATH, f"//{tag}[normalize-space(text())='{label}']/parent::td")
    return els[0].text.strip() if els else ""


def parse_account_detail(driver, account_num):
    """Owner name and mailing address off this account's detail page."""
    driver.get(ACCOUNT_URL.format(account_num=account_num))
    WebDriverWait(driver, 20).until(
        lambda d: d.find_elements(By.XPATH, "//b[normalize-space(text())='Owner Name']")
        or "unable to complete your request" in d.page_source.lower()
        or d.title == "Human Verification"
    )
    _check_not_walled(driver)
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


def process_name(driver, name, posted_date):
    last_name = extract_last_name(name)
    first_name = extract_first_name(name)
    if not last_name or not first_name:
        print(f"Skipping '{name}' - couldn't parse a first/last name.")
        return

    print(f"Searching for last name: {last_name}")
    # One long-lived session still means dozens of requests back to back
    # over a run - pace them like a real visitor rather than firing as fast
    # as the browser can go, on top of the single-session choice below.
    time.sleep(random.uniform(1.5, 3.0))
    search_last_name(driver, last_name)
    candidates = collect_candidates(driver)

    if not candidates:
        print(f"No results found for {name}. Moving on.")
        return

    for account_num, owner_name in candidates:
        owner_words = owner_name.split()
        if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
            continue

        try:
            time.sleep(random.uniform(1.0, 2.0))
            full_owner_name, street, city, state, zip_code = parse_account_detail(driver, account_num)
        except (CaptchaBlockedError, WebDriverException):
            raise
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


if __name__ == "__main__":
    pipeline_db.resolve_mode(sys.argv)  # not used here, but keeps argv[1] consistent with the rest of the pipeline

    names_with_dates = pipeline_db.get_obituary_names_with_dates()
    pipeline_db.clear_matches()

    print("Names to search:")
    for name, posted_date in names_with_dates:
        print(f"{name} ({posted_date})" if posted_date else name)

    # One browser/session for the whole run, not a fresh one per name like
    # most other counties here. This site's confirmed-live AWS WAF
    # bot-detection (see CaptchaBlockedError) reads as behavioral/rate-based
    # rather than tied to any one URL - re-logging-in as a "new guest" for
    # every single name would mean dozens of brand-new sessions from the
    # same IP in quick succession, which is exactly the kind of pattern that
    # kind of protection exists to catch. One long-lived session that just
    # keeps searching looks like one ordinary visit.
    driver = create_driver()
    try:
        for name, posted_date in names_with_dates:
            for attempt in (1, 2):
                try:
                    process_name(driver, name, posted_date)
                    break
                except CaptchaBlockedError:
                    raise
                except WebDriverException as e:
                    # The long-lived browser itself died mid-run (crash,
                    # OOM, lost connection, etc - confirmed live on Yuma
                    # County's identical-architecture $.py: a real run died
                    # this way partway through and then silently no-op'd
                    # through every remaining name with a dead session,
                    # still exiting 0 as if nothing was wrong). One retry
                    # with a brand new browser rather than losing the rest
                    # of the run to it.
                    print(f"Browser session error on '{name}' (attempt {attempt}/2): {type(e).__name__}: {e}")
                    # A bare TimeoutException has an empty message - say
                    # what page the browser was actually stuck on.
                    try:
                        body_text = driver.find_element(By.TAG_NAME, "body").text.strip().replace("\n", " | ")
                        print(f"  Stuck on: title={driver.title!r} url={driver.current_url} body={body_text[:300]!r}")
                    except Exception:
                        pass
                    if attempt == 2:
                        print(f"  Giving up on '{name}' after a fresh-browser retry.")
                        break
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_driver()
                except Exception as e:
                    print(f"Error during search for '{name}': {e}")
                    break

    except CaptchaBlockedError as e:
        print(f"\nSTOPPING: {e}")
        raise SystemExit(1)
    finally:
        driver.quit()

    print("Done.")
