import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd
import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Tells pricer.py exactly which file(s) this run of DC.py just wrote, so it
# doesn't have to guess from today's date - important in weekly mode, where
# DC.py can write up to 7 files, one per posted date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

# Legacy.com's own search-box and result-card markup is Framer-generated
# and has changed shape at least twice now (see below) - absolute paths
# into it keep going stale. The search box has no id/name, but its
# placeholder text is a stable, user-facing anchor. Confirmed live.
SEARCH_BOX_SELECTOR = 'input[placeholder="Find a loved one..."]'


def get_output_path_for_date(date_str):
    """Name the file after date_str (YYYY-MM-DD). Always the same path for
    a given date - a rerun overwrites it outright (df.to_excel() truncates
    the file), so there's only ever one file per date. dashboard.py merges
    every dated file it finds on disk, so leaving old runs' files around
    under different names let stale/outdated rows (e.g. from before a
    matching-logic bug was fixed) linger in the dashboard indefinitely."""
    return os.path.join(SCRIPT_DIR, f"{date_str}.xlsx")


def get_output_path():
    return get_output_path_for_date(datetime.now().strftime("%Y-%m-%d"))


def record_last_run(mode, paths):
    with open(LAST_RUN_PATH, "w") as f:
        json.dump({"mode": mode, "files": paths}, f)


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


def search_legacy(name, max_attempts=3):
    """Retry on failure with a brand-new browser each time. Selenium/
    undetected_chromedriver occasionally crashes mid-search (window closed,
    session died) - without a retry that silently turns into a false
    "not confirmed" instead of an actual failed lookup.

    This is the outer, whole-browser-restart safety net. The more common
    case - the top result just isn't there within the normal wait - is
    handled without a restart, inside a single attempt; see
    _search_legacy_attempt."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _search_legacy_attempt(name)
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                print(f"Error processing '{name}' (attempt {attempt}/{max_attempts}): {e}")
                print("Retrying with a fresh browser...")
                time.sleep(2)
    print(f"Giving up on '{name}' after {max_attempts} attempts: {last_error}")
    return None, None


def run_with_stale_retry(action, retries=3, delay=0.5):
    """Run `action` (a zero-arg callable that locates and interacts with an
    element), retrying on StaleElementReferenceException. Legacy.com's
    client-side rendering can swap a DOM node out from under us in the gap
    between locating an element and acting on it, which raises this even
    though the element genuinely exists a moment later."""
    for attempt in range(1, retries + 1):
        try:
            return action()
        except StaleElementReferenceException:
            if attempt == retries:
                raise
            time.sleep(delay)


def _search_legacy_attempt(name):
    print(f"Searching for '{name}' on Legacy.com...")
    options = uc.ChromeOptions()
    options.headless = False
    # Don't block driver.get() on images/ads/trackers finishing to load -
    # the WebDriverWait calls below already wait for the specific elements needed.
    options.page_load_strategy = "eager"
    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)
    # undetected_chromedriver swaps out its initial blank tab as part of its
    # own startup patching - calling .get() before that settles can raise
    # NoSuchWindowException ("target window already closed"), confirmed
    # live. Switching to whatever window handle is actually live avoids it
    # (same fix UT/uta's equivalent script already uses).
    driver.switch_to.window(driver.window_handles[-1])

    try:
        driver.get("https://www.legacy.com/")

        # 1. Search bar - type the name and click the search button.
        # Confirmed live that pressing Enter no longer submits anything at
        # all on the current site (the URL just never changes) - the actual
        # submit trigger is this button, identified by Framer's own stable
        # data-framer-name hook rather than a generated class or position.
        def do_search():
            search_box = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_BOX_SELECTOR))
            )
            search_box.click()
            search_box.send_keys(name)
            search_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-framer-name="search_button"]'))
            )
            search_button.click()

        run_with_stale_retry(do_search)

        # 2. Click the top result. Confirmed live: every result card is an
        # <article>, and the first one now links first to a legacy.com
        # "person" page that only has a truncated preview ("Read the full
        # obituary on our trusted partner sites below") - the full text is
        # inline on the direct /us/obituaries/{paper}/name/... link that's
        # also inside that same first <article>. Falls back to whatever
        # that article's own first link is (the "person" page) if no such
        # partner link exists, so the existing multi-strategy text
        # extraction below still gets a shot at it either way.
        def click_result():
            first_article = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article"))
            )
            try:
                result_link = first_article.find_element(By.CSS_SELECTOR, 'a[href*="/us/obituaries/"]')
            except NoSuchElementException:
                result_link = first_article.find_element(By.CSS_SELECTOR, "a")
            driver.get(result_link.get_attribute("href"))

        run_with_stale_retry(click_result)

        obituary_text = None
        obituary_url = None

        # 3a. Some obituary pages have a button that expands to the full
        # text - click it if present, but don't let its absence (most
        # obituaries don't have one) short-circuit past the xpath checks
        # below. Confirmed live: that's exactly what was happening - a
        # missing button raised out of this try block before the far more
        # reliable "ObituaryText" class check ever ran, even on pages where
        # that class was present the whole time and already held the full
        # text with no button needed (e.g. Theodore Capener's obituary).
        try:
            def click_expand():
                expand_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "/html/body/div[4]/div[2]/div/div/div/div/div[1]/div[1]/div[2]/div/a/div/div[1]/img"))
                )
                expand_button.click()

            run_with_stale_retry(click_expand, retries=2, delay=0.3)
        except Exception:
            pass

        # 3aa. Obituary text (possibly multiple sections), whether or not an
        # expand button was clicked above. The "ObituaryText" component
        # class is more stable than an absolute div-index path (a shift of
        # one wrapper div breaks the latter, which is exactly what happened
        # with the /div[2]/div[1] path below - the real text sits at
        # /div[3]/div[1] on the live site), so try it first.
        full_text_xpaths = [
            "//div[contains(@class, 'ObituaryText')]",
            "/html/body/div[1]/div[1]/div[4]/div[2]/div[3]/div[1]",
            "/html/body/div[1]/div[1]/div[5]/div[2]/div[1]/div/div[1]/div/div[2]",
        ]
        for xpath in full_text_xpaths:
            try:
                element = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                text = element.text.strip()
                # Skip short boilerplate (e.g. a donation blurb) that some
                # of these paths can land on instead of the real obituary.
                if text and len(text) > 100:
                    obituary_text = text
                    obituary_url = driver.current_url
                    break
            except Exception:
                continue

        # 3b. None of the above found it - try the older absolute path as a
        # last resort.
        if not obituary_text:
            try:
                element = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH,
                        "/html/body/div[4]/div[2]/div/div[2]/div/div/div[1]/div[1]/div[1]/div/p"))
                )
                obituary_text = element.text.strip()
                obituary_url = driver.current_url
            except Exception:
                obituary_text = None

        if obituary_text:
            return obituary_text, (obituary_url or driver.current_url)
        else:
            return "No obituary text found.", driver.current_url

    finally:
        driver.quit()

def extract_keyword_candidates(cleaned_full_name, cleaned_owner_name):
    """The owner record's >=4-char "extra" tokens (spouse name, middle
    name, etc.) left over after subtracting the deceased's own name -
    someone the obituary would independently mention if this really is the
    right property record."""
    full_tokens = {w.lower() for w in (cleaned_full_name or "").split()}
    owner_tokens = {w.lower() for w in (cleaned_owner_name or "").split()}
    return sorted(owner_tokens - full_tokens)


# 2026-09-22 accuracy fix: this file used to have check_initial_vs_own_name()
# (plus its supporting extract_lone_initials()/middle_name_tokens()/
# _strip_nickname_and_suffix()/GENERATIONAL_SUFFIXES/extract_fuller_name_
# from_obituary() machinery) that "confirmed" a match whenever a lone
# initial in the owner record matched the first letter of the deceased's
# OWN middle name - even retrying against a fuller name pulled out of the
# obituary text itself when the first try missed. Both variants compare
# the record to itself, not to anything external: a living namesake (a
# son, a father, a cousin with the same name in the same county) passes
# either check identically to the actual deceased person. Removed outright
# - see confirm_match() below, which requires the obituary text to
# actually name a co-owner/spouse or the property's own city before
# anything is called confirmed.


def has_anything_to_check(cleaned_full_name, cleaned_owner_name, city=""):
    """Whether there's a >=4-char keyword or a real city on file worth
    searching the obituary text for. If neither exists, no amount of
    searching could corroborate this specific owner record, so the
    (expensive) Legacy.com search is skipped."""
    if extract_keyword_candidates(cleaned_full_name, cleaned_owner_name):
        return True
    return bool(city) and len(city) >= 4


def confirm_match(cleaned_full_name, cleaned_owner_name, obituary_text, city=""):
    """A lead is confirmed only when the obituary text itself corroborates
    THIS SPECIFIC property record - never by comparing the record to
    itself. Two independent signals count:
      1. A co-owner/spouse name left over in the owner record (see
         extract_keyword_candidates) appears as a whole word in the
         obituary text.
      2. Failing that, the property's own on-file city appears as a whole
         word in the obituary text (e.g. "...of Salt Lake City, Utah") -
         real corroboration even for a single-owner record with no
         separate co-owner name to check.
    Whole-word matching (\\b...\\b), not a bare substring test, so "mark"
    doesn't match inside "remarkable" and "june" doesn't match inside
    "junction" - the substring version of this check produced exactly
    those false hits. (A name that IS a common word or a month - an actual
    "June" - can still coincidentally match its literal use elsewhere in
    the text; that ambiguity is real and not something a word-boundary
    check can resolve.)"""
    keyword_candidates = extract_keyword_candidates(cleaned_full_name, cleaned_owner_name)
    print(f"  Searching obituary for: {', '.join(keyword_candidates) if keyword_candidates else '(none)'}"
          + (f" or city '{city}'" if city else ""))

    haystack = (obituary_text or "").lower()
    matched = [kw for kw in keyword_candidates if re.search(rf"\b{re.escape(kw)}\b", haystack)]
    if not matched and city and len(city) >= 4:
        if re.search(rf"\b{re.escape(city.lower())}\b", haystack):
            matched = [f"city:{city}"]
    return sorted(matched)


if __name__ == "__main__":
    mode = pipeline_db.resolve_mode(sys.argv)

    # Confirm every candidate match SLK$.py found.
    matches = pipeline_db.get_matches_full()

    if not matches:
        print("No candidate matches to confirm yet. Run SLK$.py first.")
        record_last_run(mode, [])
    else:
        obituary_cache = {}
        confirmed_rows = []

        for match_id, full_name, owner_name, parcel_id, cleaned_full_name, cleaned_owner_name, posted_date, address, city, state, zip_code in matches:
            def confirm(reason, obituary_url=""):
                pipeline_db.set_match_confirmation(match_id, True, reason)
                print(f"Confirmed: {full_name} / owner '{owner_name}' — found: {reason}")
                row = {
                    "Full Name": full_name,
                    "Owner Name": owner_name,
                    "Parcel ID": parcel_id,
                    "Address": address or "",
                    "City": city or "",
                    "State": state or "",
                    "Zip": zip_code or "",
                    "Matched Keywords": reason,
                    "Obituary URL": obituary_url,
                }
                if mode == "weekly":
                    row["Posted Date"] = posted_date or ""
                confirmed_rows.append(row)

            if not has_anything_to_check(cleaned_full_name, cleaned_owner_name, city):
                print(f"Skipping obituary search for '{full_name}' / owner '{owner_name}' — no keyword or city to check against.")
                pipeline_db.set_match_confirmation(match_id, False, "")
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — nothing to check against the obituary")
                continue

            if full_name not in obituary_cache:
                text, url = search_legacy(full_name)
                obituary_cache[full_name] = (text or "", url)
                pipeline_db.set_obituary_url(full_name, url)

            obituary_text, obituary_url = obituary_cache[full_name]
            matched_keywords = confirm_match(cleaned_full_name, cleaned_owner_name, obituary_text, city)
            reason = ", ".join(matched_keywords)

            pipeline_db.set_match_confirmation(match_id, bool(reason), reason)

            if reason:
                confirm(reason, obituary_url)
            else:
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — no corroborating keyword or city found in obituary")

        print(f"\nChecked {len(matches)} candidate matches against obituary text.")

        # 2026-09-22 accuracy fix: if the same decedent still ended up with
        # more than one CONFIRMED parcel under different owner names, that's
        # not corroboration - it's SLK$.py's own loose first-name-prefix
        # search turning up more than one plausible property and this
        # file's keyword/city check happening to clear more than one of
        # them. There's no reliable way to guess which is actually right,
        # so none of them ship as confirmed - dropped here rather than
        # guessed at. A real "needs human review" queue for cases like this
        # is a reasonable next step, not yet built.
        rows_by_name = {}
        for row in confirmed_rows:
            rows_by_name.setdefault(row["Full Name"], []).append(row)
        ambiguous_names = {
            name for name, rows in rows_by_name.items()
            if len({r["Owner Name"] for r in rows}) > 1
        }
        if ambiguous_names:
            for name in sorted(ambiguous_names):
                owners = sorted({r["Owner Name"] for r in rows_by_name[name]})
                print(f"Ambiguous: '{name}' confirmed against {len(owners)} different owner records ({'; '.join(owners)}) - holding all of them back rather than guessing.")
            confirmed_rows = [row for row in confirmed_rows if row["Full Name"] not in ambiguous_names]

        written_paths = []

        if mode == "weekly":
            columns = ["Full Name", "Owner Name", "Parcel ID", "Address", "City", "State", "Zip", "Matched Keywords", "Obituary URL", "Posted Date"]
            rows_by_date = {}
            for row in confirmed_rows:
                date_key = row["Posted Date"] or datetime.now().strftime("%Y-%m-%d")
                rows_by_date.setdefault(date_key, []).append(row)

            if not rows_by_date:
                print("No confirmed matches this week - nothing to export.")
            for date_key, rows in sorted(rows_by_date.items()):
                df = pd.DataFrame(rows, columns=columns)
                output_path = get_output_path_for_date(date_key)
                df.to_excel(output_path, index=False)
                written_paths.append(output_path)
                print(f"Exported {len(df)} confirmed matches posted {date_key} to {output_path}")
        else:
            columns = ["Full Name", "Owner Name", "Parcel ID", "Address", "City", "State", "Zip", "Matched Keywords", "Obituary URL"]
            df = pd.DataFrame(confirmed_rows, columns=columns)
            output_path = get_output_path()
            df.to_excel(output_path, index=False)
            written_paths.append(output_path)
            print(f"Exported {len(df)} confirmed matches to {output_path}")

        record_last_run(mode, written_paths)
