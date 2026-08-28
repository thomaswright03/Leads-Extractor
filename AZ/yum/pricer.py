import json
import os
import random
import re
import shutil
import subprocess
import sys
import time

import pandas as pd
import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CHROME_BINARY_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Written by DC.py at the end of its run - lists exactly the file(s) it just
# produced (one "today" file in daily mode, or one per posted date in weekly
# mode), so this script doesn't have to guess from today's date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

# Parcel ID here is the EagleWeb/TaxWeb account number (e.g. "R000467"),
# same convention as apa/pricer.py on this identical platform - $.py stores
# that instead of the human-readable parcel number because it's a direct
# deep link straight to this account's page, no separate search step needed.
ACCOUNT_URL = "https://yumacountyaz-assessor.tylerhost.net/assessor/taxweb/account.jsp?accountNum={parcel_id}&guest=true"
SEARCH_URL = "https://yumacountyaz-assessor.tylerhost.net/assessor/taxweb/search.jsp"
GUEST_LOGIN_XPATH = "//form[.//input[@name='guest']]//input[@type='submit']"


def get_last_run():
    """The mode and exact file(s) DC.py produced on its last run, per its
    manifest - this is what actually determines what there is to price,
    regardless of which mode is chosen when running pricer.py stand-alone."""
    if not os.path.exists(LAST_RUN_PATH):
        return None, []
    with open(LAST_RUN_PATH) as f:
        data = json.load(f)
    paths = [p for p in data.get("files", []) if os.path.exists(p)]
    return data.get("mode"), paths


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
    # undetected_chromedriver, not plain webdriver.Chrome() - this site's
    # own confirmed-live AWS WAF bot-detection (see $.py) applies here too.
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


def _account_not_found(driver):
    # A bad account number doesn't 404 or show a labeled error on this
    # account/detail endpoint - it renders EagleWeb's generic "Sorry, I was
    # unable to complete your request" page instead (confirmed live on
    # Apache's identical deployment of this platform).
    return "unable to complete your request" in driver.page_source.lower()


def _fcv_text(driver):
    # The Full Cash Value figure sits in the table cell right after the one
    # holding its bold label, not inside the labeled cell itself (unlike
    # Owner Name/Owner Address elsewhere on this same page) - confirmed live.
    els = driver.find_elements(
        By.XPATH, "//td[b[normalize-space(text())='Full Cash Value (FCV)']]/following-sibling::td[1]"
    )
    return els[0].text.strip() if els and els[0].text.strip() else None


def _ensure_guest_session(driver):
    """Establishes a guest session once for the whole run (see $.py for why
    this reuses one session rather than a fresh one per lookup: this site's
    confirmed-live AWS WAF bot-detection reads as rate/behavior-based, and
    logging in fresh for every single parcel would look like a burst of
    brand-new visitors from the same IP)."""
    driver.get(f"{SEARCH_URL}?guest=true")
    if driver.find_elements(By.ID, "OwnerIDSearchString"):
        return
    guest_buttons = driver.find_elements(By.XPATH, GUEST_LOGIN_XPATH)
    if guest_buttons:
        guest_buttons[0].click()
        WebDriverWait(driver, 20).until(lambda d: d.find_elements(By.ID, "OwnerIDSearchString"))


def _new_driver_with_session(max_attempts=3):
    """Create a driver and establish its guest session, retrying the whole
    thing on a transient failure here rather than letting it crash the
    entire run unhandled - confirmed live: a bare net::ERR_CONNECTION_RESET
    hitting this exact step (not a lookup, just the initial/recovery
    session setup) previously killed the whole script even with confirmed
    leads already sitting there waiting to be priced."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        driver = create_driver()
        try:
            _ensure_guest_session(driver)
            return driver
        except Exception as e:
            last_exc = e
            print(f"Couldn't establish a session (attempt {attempt}/{max_attempts}): {e}")
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
    raise last_exc


def get_property_value(driver, parcel_id):
    driver.get(ACCOUNT_URL.format(parcel_id=parcel_id))
    if driver.title == "Login" or driver.find_elements(By.XPATH, GUEST_LOGIN_XPATH):
        # The guest session from _ensure_guest_session() expired or never
        # took - re-establish it and retry this one lookup once.
        _ensure_guest_session(driver)
        driver.get(ACCOUNT_URL.format(parcel_id=parcel_id))
    WebDriverWait(driver, 20).until(lambda d: _fcv_text(d) or _account_not_found(d) or d.title == "Human Verification")

    if driver.title == "Human Verification":
        raise RuntimeError(
            "Yuma County's assessor site put up its AWS WAF 'Human "
            "Verification' CAPTCHA wall - stopping rather than trying to "
            "solve it. Retry later with more spacing between requests."
        )
    if _account_not_found(driver):
        raise ValueError(f"Account {parcel_id} not found on assessor site.")

    value = _fcv_text(driver)
    if not value:
        raise ValueError(f"Account {parcel_id}: value element never populated.")
    return value


if __name__ == "__main__":
    mode = pipeline_db.resolve_mode(sys.argv)
    last_run_mode, matches_paths = get_last_run()

    if not matches_paths:
        print("No DC.py output found to price. Run DC.py first.")
        raise SystemExit(0)

    if last_run_mode and last_run_mode != mode:
        print(f"Note: DC.py's last run was '{last_run_mode}' mode - pricing those {len(matches_paths)} file(s) regardless of the mode picked here.")

    driver = _new_driver_with_session()
    try:
        for matches_path in matches_paths:
            df = pd.read_excel(matches_path)
            if df.empty:
                print(f"{matches_path} has no matches - nothing to price.")
                continue

            property_values = []
            for _, row in df.iterrows():
                parcel_id = str(row.get("Parcel ID", "")).strip()
                full_name = row.get("Full Name", "")
                address, city, state, zip_code = pipeline_db.get_address_by_parcel_id(parcel_id)

                if not address:
                    print(f"No address on file for '{full_name}' (Parcel ID {parcel_id}) - skipping.")
                    property_values.append("No address on file")
                    continue

                print(f"Looking up property value for '{full_name}': Parcel ID {parcel_id}")
                value = None
                for attempt in (1, 2):
                    try:
                        value = get_property_value(driver, parcel_id)
                        print(f"  -> {value}")
                        break
                    except WebDriverException as e:
                        # The long-lived browser itself died mid-run (crash,
                        # OOM, lost connection, etc - confirmed live on
                        # $.py's identical single-session architecture). One
                        # retry with a brand new browser+session rather than
                        # losing every remaining row to a dead session.
                        print(f"  -> Browser session error (attempt {attempt}/2): {e}")
                        if attempt == 2:
                            break
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = _new_driver_with_session()
                    except Exception as e:
                        print(f"  -> Failed to get property value: {e}")
                        break
                property_values.append(value or "Not found")

                time.sleep(random.uniform(1.5, 3.0))

            df["Property Value"] = property_values
            df.to_excel(matches_path, index=False)
            print(f"Updated {matches_path} with {len(property_values)} property value(s).")
    finally:
        driver.quit()
