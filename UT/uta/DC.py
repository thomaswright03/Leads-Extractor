import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd
import undetected_chromedriver as uc
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Tells pricer.py exactly which file(s) this run of DC.py just wrote, so it
# doesn't have to guess from today's date - important in weekly mode, where
# DC.py can write up to 7 files, one per posted date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

# The result-card layout Legacy.com renders for the 2nd result differs from
# the 1st - confirmed by hand against the live site, not guessable from a
# single formula.
RESULT_LINK_XPATHS = {
    1: "/html/body/div[4]/div[2]/div[2]/div[1]/article/div[1]/div/a/div/div",
    2: "/html/body/div[4]/div[2]/div[2]/div[2]/div[2]/div/article[1]/div[1]/div/a/div/div",
}

# The "read the full obituary" link on Legacy.com's own result page - stable
# across virtually every listing regardless of which external funeral-home
# site (Dignity Memorial, a local funeral home's own site, etc.) it actually
# points to.
FULL_OBITUARY_LINK_XPATH = "/html/body/div[4]/div[2]/div/div[2]/div/div/div[1]/div[1]/div[2]/div/a/div"


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
    session entirely. Pin it to the real installed major version instead."""
    try:
        output = subprocess.check_output([CHROME_BINARY, "--version"], text=True)
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    except Exception:
        pass
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

    try:
        driver.get("https://www.legacy.com/")

        # 1. Search bar - type the name and press Enter
        def do_search():
            search_box = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH,
                    "/html/body/div[1]/div[1]/div[3]/div[2]/div[2]/div/div/div[2]/div[2]/div/div/input"))
            )
            search_box.click()
            search_box.send_keys(name)
            search_box.send_keys(Keys.RETURN)

        run_with_stale_retry(do_search)

        # 2. Click the top result. If it's not there within the normal 10s
        # wait, don't reload or restart the browser - stay on this same
        # results page and try the 2nd result's spot instead.
        def click_result():
            try:
                result_link = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, RESULT_LINK_XPATHS[1]))
                )
            except TimeoutException:
                print(f"Top result not found within 10s for '{name}' - trying the 2nd result on the same page...")
                result_link = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, RESULT_LINK_XPATHS[2]))
                )
            result_link.click()

        run_with_stale_retry(click_result)

        obituary_text = None
        obituary_url = None

        # 3a. Most listings have a "read the full obituary" link that leads
        # off to one of a dozen-plus different external funeral-home/obituary
        # sites, each with completely different markup - rather than chase
        # per-site XPaths across all of them, just grab everything visible on
        # whatever page it lands on and let confirm_match() search that whole
        # blob of text for the keyword(s).
        try:
            pre_click_url = driver.current_url
            pre_click_handles = driver.window_handles

            def click_full_obituary_link():
                link = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, FULL_OBITUARY_LINK_XPATH))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                link.click()

            run_with_stale_retry(click_full_obituary_link, retries=2, delay=0.3)

            # The link either opens the destination site in a new tab or
            # navigates the current one, depending on which external site it
            # points to - wait for whichever happens rather than assuming one.
            WebDriverWait(driver, 10).until(
                lambda d: len(d.window_handles) > len(pre_click_handles) or d.current_url != pre_click_url
            )

            if len(driver.window_handles) > len(pre_click_handles):
                new_window = [h for h in driver.window_handles if h not in pre_click_handles][0]
                driver.switch_to.window(new_window)

            time.sleep(1)  # let the destination page finish rendering
            obituary_text = driver.find_element(By.TAG_NAME, "body").text.strip()
            obituary_url = driver.current_url

        except Exception:
            # 3b. No full-obituary link on this listing (or it didn't respond
            # in time) - the obituary text is directly on the result page.
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

# Same punctuation set pipeline_db.clean_name() strips before splitting -
# duplicated here because clean_name() throws away single-letter tokens
# (middle initials), but that's exactly what extract_lone_initials() needs.
STRIP_CHARS = '(),"\'.!?;'


def extract_keyword_candidates(cleaned_full_name, cleaned_owner_name):
    """The owner record's >=4-char "extra" tokens (spouse name, middle
    name, etc.) left over after subtracting the deceased's own name."""
    full_tokens = {w.lower() for w in (cleaned_full_name or "").split()}
    owner_tokens = {w.lower() for w in (cleaned_owner_name or "").split()}
    return sorted(owner_tokens - full_tokens)


def extract_lone_initials(raw_owner_name):
    """Single-letter tokens (e.g. the 'G' in 'BANYAI, JACOB G; TR') found in
    the raw owner-name string - typically a middle initial. clean_name()
    drops these (it only keeps words >=4 chars), so they have to be pulled
    from the raw string, not the cleaned one."""
    cleaned = raw_owner_name or ""
    for ch in STRIP_CHARS:
        cleaned = cleaned.replace(ch, "")
    return sorted({w.lower() for w in cleaned.split() if len(w) == 1 and w.isalpha()})


def middle_name_tokens(full_name):
    """The deceased's own name tokens other than the first and last word -
    e.g. 'Gregory' out of 'Jacob Gregory Banyai'. This is already known
    locally from the obituary listing itself, no Legacy.com search needed."""
    parts = (full_name or "").split()
    if len(parts) <= 2:
        return []
    return parts[1:-1]


def check_initial_vs_own_name(owner_name, full_name):
    """A lone initial in the owner record (e.g. the 'G' in 'JACOB G') very
    often just stands for the deceased's own middle name, which we already
    have from the obituary listing - no search needed to confirm that.
    Returns (initial, matched_word) if a lone initial matches the first
    letter of one of the deceased's own middle name(s), else None."""
    lone_initials = extract_lone_initials(owner_name)
    if not lone_initials:
        return None
    for word in middle_name_tokens(full_name):
        lower_word = word.lower()
        if lower_word and lower_word[0] in lone_initials:
            return lower_word[0], word
    return None


def has_anything_to_check(cleaned_full_name, cleaned_owner_name):
    """Whether there's a >=4-char keyword worth searching the obituary
    text for. A lone initial alone isn't a reason to search - that's
    already been checked against the deceased's own known name before this
    point (see check_initial_vs_own_name); if it didn't match there, there
    is nothing left an obituary search could confirm from it. (Matching a
    lone initial against arbitrary capitalized words in the full obituary
    article - not just the deceased's own name - was tried and produced
    garbage hits like "R matches 'Rally'" or "M matches 'Memory'", so that
    path was removed rather than kept as a fallback.)"""
    return bool(extract_keyword_candidates(cleaned_full_name, cleaned_owner_name))


def confirm_match(cleaned_full_name, cleaned_owner_name, obituary_text):
    """Check whether the owner record's >=4-char "extra" word (spouse
    name, middle name, etc, left over after subtracting the deceased's own
    name) shows up as a literal substring in the obituary text."""
    keyword_candidates = extract_keyword_candidates(cleaned_full_name, cleaned_owner_name)
    print(f"  Searching obituary for: {', '.join(keyword_candidates) if keyword_candidates else '(none)'}")

    haystack = (obituary_text or "").lower()
    return sorted(kw for kw in keyword_candidates if kw in haystack)


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
            # Cheapest check first, before any Legacy.com search: does the
            # assessor record's lone initial just match the deceased's own
            # already-known middle name? If so, that's confirmed on its own
            # - no need to spend a search on it.
            own_name_hit = check_initial_vs_own_name(owner_name, full_name)
            if own_name_hit:
                initial, hit_word = own_name_hit
                reason = f"initial {initial.upper()} matches deceased's own name '{hit_word}'"
                pipeline_db.set_match_confirmation(match_id, True, reason)
                print(f"Confirmed: {full_name} / owner '{owner_name}' — found: {reason} (no Legacy.com search needed)")
                row = {
                    "Full Name": full_name,
                    "Owner Name": owner_name,
                    "Parcel ID": parcel_id,
                    "Address": address or "",
                    "City": city or "",
                    "State": state or "",
                    "Zip": zip_code or "",
                    "Matched Keywords": reason,
                    "Obituary URL": pipeline_db.get_obituary_url(full_name) or "",
                }
                if mode == "weekly":
                    row["Posted Date"] = posted_date or ""
                confirmed_rows.append(row)
                continue

            if not has_anything_to_check(cleaned_full_name, cleaned_owner_name):
                print(f"Skipping obituary search for '{full_name}' / owner '{owner_name}' — no keyword to check against.")
                pipeline_db.set_match_confirmation(match_id, False, "")
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — nothing to check against the obituary")
                continue

            if full_name not in obituary_cache:
                text, url = search_legacy(full_name)
                obituary_cache[full_name] = (text or "", url)
                pipeline_db.set_obituary_url(full_name, url)

            obituary_text, obituary_url = obituary_cache[full_name]
            matched_keywords = confirm_match(cleaned_full_name, cleaned_owner_name, obituary_text)
            reason = ", ".join(matched_keywords)

            pipeline_db.set_match_confirmation(match_id, bool(reason), reason)

            if reason:
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
            else:
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — no corroborating keyword or initial found in obituary")

        print(f"\nChecked {len(matches)} candidate matches against obituary text.")

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
