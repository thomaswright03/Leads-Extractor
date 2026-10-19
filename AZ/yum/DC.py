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
from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException
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
# and has changed shape at least twice now - absolute paths into it keep
# going stale. The search box has no id/name, but its placeholder text is
# a stable, user-facing anchor. Confirmed live (same fix already verified
# and shipped in UT/slk/DC.py - this file just hadn't been updated yet).
SEARCH_BOX_SELECTOR = 'input[placeholder="Find a loved one..."]'

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


def record_last_run(mode, paths, stats=None):
    """stats (candidate/lookup/confirmed counts) is shown on the /admin
    page in dashboard.py; pricer.py only reads "files"."""
    with open(LAST_RUN_PATH, "w") as f:
        json.dump({"mode": mode, "files": paths, "stats": stats or {}}, f)


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
            # Since ~2026-09-22 an invisible full-screen overlay (<div
            # aria-hidden="true" style="position: fixed; inset: 0px;">)
            # sits over the button once the name is typed, so a real click
            # raised ElementClickInterceptedException on every lookup and
            # nothing got confirmed. A DOM click goes straight to the button.
            try:
                search_button.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", search_button)

        run_with_stale_retry(do_search)

        # 2. Click the top result. Confirmed live: every result card is an
        # <article> - grab whichever link that article actually has (its
        # own first link, whatever that is) rather than a fragile absolute
        # path into it.
        def click_result():
            first_article = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article"))
            )
            result_link = first_article.find_element(By.CSS_SELECTOR, "a")
            driver.get(result_link.get_attribute("href"))

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

def extract_keyword_candidates(cleaned_full_name, cleaned_owner_name):
    """The owner record's >=4-char "extra" tokens (spouse name, middle
    name, etc.) left over after subtracting the deceased's own name -
    someone the obituary would independently mention if this really is the
    right property record."""
    full_tokens = {w.lower() for w in (cleaned_full_name or "").split()}
    owner_tokens = {w.lower() for w in (cleaned_owner_name or "").split()}
    return sorted(owner_tokens - full_tokens)


# 2026-09-22 accuracy fix: this file used to have a check_initial_vs_own_name()
# that "confirmed" a match whenever a lone initial in the owner record (e.g.
# the "G" in "JACOB G") matched the first letter of the deceased's OWN
# middle name - without ever reading the obituary. That's comparing the
# record to itself, not corroborating it against anything external: 521 of
# 533 leads at one point were confirmed this way, and a living namesake (a
# son, a father, a cousin with the same name in the same county) passes
# that check identically to the actual deceased person. It's been removed
# outright - see confirm_match() below, which requires the obituary text
# to actually name a co-owner/spouse or the property's own city before
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
         word in the obituary text (e.g. "...of Springville, Utah") - real
         corroboration even for a single-owner record with no separate
         co-owner name to check.
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

    # Confirm every candidate match $.py found.
    matches = pipeline_db.get_matches_full()

    if not matches:
        print("No candidate matches to confirm yet. Run $.py first.")
        record_last_run(mode, [], {"candidates": 0, "lookups_attempted": 0, "lookups_failed": 0, "confirmed": 0})
    else:
        obituary_cache = {}
        confirmed_rows = []
        lookups_attempted = 0
        lookups_failed = 0

        for match_id, full_name, owner_name, parcel_id, cleaned_full_name, cleaned_owner_name, posted_date, address, city, state, zip_code in matches:
            if not has_anything_to_check(cleaned_full_name, cleaned_owner_name, city):
                print(f"Skipping obituary search for '{full_name}' / owner '{owner_name}' — no keyword or city to check against.")
                pipeline_db.set_match_confirmation(match_id, False, "")
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — nothing to check against the obituary")
                continue

            if full_name not in obituary_cache:
                text, url = search_legacy(full_name)
                lookups_attempted += 1
                if url is None:
                    lookups_failed += 1
                obituary_cache[full_name] = (text or "", url)
                pipeline_db.set_obituary_url(full_name, url)

            obituary_text, obituary_url = obituary_cache[full_name]
            matched_keywords = confirm_match(cleaned_full_name, cleaned_owner_name, obituary_text, city)
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
                print(f"Not confirmed: {full_name} / owner '{owner_name}' — no corroborating keyword or city found in obituary")

        print(f"\nChecked {len(matches)} candidate matches against obituary text.")

        # 2026-09-22 accuracy fix: if the same decedent still ended up with
        # more than one CONFIRMED parcel under different owner names, that's
        # not corroboration - it's the search step (which matches loosely by
        # first-name prefix) turning up more than one plausible property and
        # this file's own keyword/city check happening to clear more than
        # one of them. There's no reliable way to guess which is actually
        # right, so - per the fix this replaces "an initial alone never
        # confirms" with - none of them ship as confirmed. They're dropped
        # here rather than guessed at; a real "needs human review" queue for
        # cases like this is a reasonable next step, not yet built.
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

        record_last_run(mode, written_paths, {
            "candidates": len(matches),
            "lookups_attempted": lookups_attempted,
            "lookups_failed": lookups_failed,
            "confirmed": len(confirmed_rows),
        })

        # Every lookup erroring out means Legacy.com changed under us, not
        # that there were no leads - exactly what went unnoticed from
        # 2026-09-22 to 2026-10-19 while every run still showed green. Exit
        # non-zero (after the export above, so nothing is lost) so the
        # county's step fails and the nightly run goes red.
        if lookups_attempted and lookups_failed == lookups_attempted:
            print(f"\nERROR: all {lookups_attempted} Legacy.com lookups failed - no match could be checked.")
            sys.exit(1)
