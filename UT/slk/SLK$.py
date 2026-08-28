import pandas as pd
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
import sys
import time
import re
import shutil
import subprocess
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os  # <-- Added for file deletion
from urllib.parse import urlparse, parse_qs

import pipeline_db
import geocode

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

# Four straight fix attempts (webdriver_manager removal, ~/Downloads
# creation, --disable-gpu, --disable-popup-blocking) all left the exact same
# 100%-failure pattern unchanged in CI, and the blank "Message: " on every
# failure is consistent with a bare TimeoutException giving no real
# diagnostic info - unbuffered, timestamped output so the next CI run's log
# actually shows real per-search timing and the real exception type instead
# of a buffered dump of "Error during search... Message: " with nothing to
# go on.
sys.stdout.reconfigure(line_buffering=True)


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# Path to the downloaded Excel file to process and later delete
downloaded_excel_file = os.path.expanduser('~/Downloads/pt1091601.xls')
download_dir = os.path.dirname(downloaded_excel_file)
# CI runners have no desktop environment, so ~/Downloads doesn't exist by
# default - Chrome's "download.default_directory" preference pointing at a
# nonexistent path made every single browser launch crash instantly with an
# empty WebDriverException (confirmed live: SLKobi.py, which sets no
# download preference at all, ran fine in the same job).
os.makedirs(download_dir, exist_ok=True)

# Load the names to search from the shared cache (populated by SLKobi copy.py)
names_with_dates = pipeline_db.get_obituary_names_with_dates()

# Start this run's match cache fresh
pipeline_db.clear_matches()

print("Names to search:")
for name, posted_date in names_with_dates:
    print(f"{name} ({posted_date})" if posted_date else name)

GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _strip_suffix_clause(full_name):
    """Obituary listing names occasionally carry a trailing rank/title
    clause after a comma (e.g. "David J Kitchin III, Lt Col/R") that isn't
    part of the actual name - left in, it gets treated as the last word and
    searched as a last name that doesn't exist. A comma-separated
    generational suffix (e.g. "Robert Smith, Jr.") is kept, since that's
    part of the actual name."""
    name, sep, suffix = full_name.partition(",")
    if sep and suffix.strip().rstrip(".").lower() not in GENERATIONAL_SUFFIXES:
        return name.strip()
    return full_name.strip()


def extract_last_name(full_name):
    name_parts = _strip_suffix_clause(full_name).split()
    while name_parts and name_parts[-1].lower().rstrip(".") in GENERATIONAL_SUFFIXES:
        name_parts = name_parts[:-1]
    name_parts = re.sub(r'\(.*\)$', '', ' '.join(name_parts)).split()
    return name_parts[-1] if name_parts else ''

def safe_click(driver, element):
    """Scroll an element into view and click it, falling back to a JS click
    if something (an ad, a sticky banner) is overlapping the target point."""
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def extract_first_name(full_name):
    name_parts = _strip_suffix_clause(full_name).split()
    if len(name_parts) > 1:
        return name_parts[0].capitalize()
    return ''

results = []

for index, (name, posted_date) in enumerate(names_with_dates):
    last_name = extract_last_name(name)
    first_name = extract_first_name(name)

    options = uc.ChromeOptions()
    # options.add_argument('--headless')  # Uncomment if you want headless
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.page_load_strategy = 'eager'
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # The search button's onclick handler opens the results in a new window
    # via window.open() (confirmed live: it built the right resultsMain.cfm
    # URL every time) - Chrome's popup blocker was silently swallowing that
    # window.open() every single run, so `len(driver.window_handles) > 1`
    # below never became true and every search timed out with a blank
    # TimeoutException. That's the real cause of the 100% failure rate; the
    # earlier GPU/webdriver_manager/Downloads-dir fixes were treating a
    # crash that wasn't actually happening.
    options.add_argument('--disable-popup-blocking')
    options.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })

    # undetected_chromedriver, not plain webdriver.Chrome(), for consistency
    # with every other driver-creating script in this pipeline.
    chrome_major = detect_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=chrome_major) if chrome_major else uc.Chrome(options=options)

    try:
        _log(f"Searching for last name: {last_name}")
        driver.get("https://www.saltlakecounty.gov/assessor/")
        _log(f"  loaded {driver.current_url!r}, title={driver.title!r}")
        time.sleep(0.5)

        search_box_owner = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "OwnerSearch"))
        )
        _log("  found OwnerSearch box")
        search_box_owner.clear()
        search_box_owner.send_keys(last_name)

        # This absolute XPath is the actual, confirmed root cause of every
        # search failing: verified live against the real site that it now
        # matches zero elements (a site redesign moved the button), while
        # the button's own id="BeginSearch" still exists and is visible -
        # OwnerSearch above already used an id lookup, which is exactly why
        # that half kept working while this one silently never found the
        # button and timed out, every single search, regardless of any
        # Chrome flag.
        search_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.ID, "BeginSearch"))
        )
        _log("  found search button")
        driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
        time.sleep(0.5)
        search_button.click()
        _log(f"  clicked search, window_handles={len(driver.window_handles)}")

        time.sleep(3)
        _log(f"  after 3s sleep, window_handles={len(driver.window_handles)}, current_url={driver.current_url!r}")

        WebDriverWait(driver, 20).until(lambda d: len(d.window_handles) > 1)
        driver.switch_to.window(driver.window_handles[-1])

        # Added section: check for no results or single result page

        try:
            no_results_element = driver.find_element(By.XPATH, '/html/body/section[4]/div/div/main/section/div/div/p')
            no_results_text = no_results_element.text.strip()
            if "Your search returned no results" in no_results_text:
                print(f"No results found for {name}. Moving on.")
                driver.quit()
                continue
        except:
            pass

        try:
            h2_elements = driver.find_elements(By.TAG_NAME, "h2")
            header_text = h2_elements[0].text.strip() if h2_elements else ""
            if header_text == "Parcel Details":
                # The parcel details page (apps.saltlakecounty.gov/.../valuationInfoExpanded.cfm)
                # renders as plain unlabeled divs/table rows rather than the nested
                # table structure the old absolute XPaths assumed, so pull the
                # parcel id straight from the URL and match the owner row by its
                # label text instead of a brittle fixed path.
                parcel_id = parse_qs(urlparse(driver.current_url).query).get("parcel_id", [""])[0]

                owner_name = ""
                address = ""
                for tr in driver.find_elements(By.TAG_NAME, "tr"):
                    cells = tr.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 2:
                        continue
                    label = cells[0].text.strip().lower()
                    if label == "owner":
                        owner_name = cells[1].text.strip()
                    elif label == "address":
                        address = cells[1].text.strip()

                owner_name_parts = owner_name.split()
                if any(word.lower().startswith(first_name.lower()) for word in owner_name_parts):
                    city, state, zip_code = geocode.geocode_address(address)
                    print(f"Single result found for {name}: Owner = {owner_name}, Parcel ID = {parcel_id}, Address = {address}")
                    results.append({
                        "Full Name": name,
                        "Owner Name": owner_name,
                        "Parcel ID": parcel_id,
                        "Address": address,
                        "City": city,
                        "State": state,
                        "Zip": zip_code,
                    })
                    pipeline_db.add_match(name, owner_name, parcel_id, address, city, state, zip_code, posted_date)
                else:
                    print(f"Single result found for {name} but owner '{owner_name}' doesn't match first name '{first_name}'; skipping.")
                driver.quit()
                continue
        except:
            pass

        # Download the Excel file
        if os.path.exists(downloaded_excel_file):
            os.remove(downloaded_excel_file)
            print(f"Deleted the file: {downloaded_excel_file}")
        else:
            print(f"File not found, could not delete: {downloaded_excel_file}")

        excel_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "/html/body/section[4]/div/div/main/section/div/div[1]/div[2]/a"))
        )
        safe_click(driver, excel_link)
        print("Clicked the 'MS Excel Document' link.")

        # Clicking the link above runs showExcel(), a jQuery AJAX call that
        # briefly replaces #excel's content with a real <a href="....xls">
        # <img></a> plus a live "link expires in N seconds" countdown -
        # confirmed live that once it hits 0, the whole subtree reverts back
        # to a loading spinner, which is what was making the previous
        # click-the-image approach fail with StaleElementReferenceException
        # (the element could vanish mid-click, or between finding it and
        # clicking it, since WebDriverWait doesn't retry on that exception
        # by default). The resolved href itself is stable the whole time, so
        # read it and navigate straight there instead of racing a click
        # against a self-destructing countdown widget.
        def _find_download_href(d):
            try:
                return d.find_element(By.CSS_SELECTOR, "#excel a").get_attribute("href")
            except Exception:
                return None

        download_url = WebDriverWait(driver, 10).until(_find_download_href)
        driver.get(download_url)
        print(f"Navigated to the download link: {download_url}")

        # Wait for the download to actually land instead of guessing a fixed delay
        wait_seconds, poll_interval, elapsed = 20, 0.5, 0
        while elapsed < wait_seconds:
            if os.path.exists(downloaded_excel_file) and not os.path.exists(downloaded_excel_file + ".crdownload"):
                break
            time.sleep(poll_interval)
            elapsed += poll_interval

        if not os.path.exists(downloaded_excel_file):
            print(f"Download never landed at {downloaded_excel_file} after {wait_seconds}s; skipping {name}.")
            driver.quit()
            continue

        # Now load and process the downloaded Excel file for matches
        parcel_df = pd.read_excel(downloaded_excel_file)

        # Iterate rows in downloaded file and apply matching logic
        for _, row in parcel_df.iterrows():
            owner_name = str(row['OWNER_NAME']).strip()
            parcel_id = str(row['PARCEL_ID']).strip()
            address = str(row.get('PROPERTY_LOCATION', '')).strip()
            if address.lower() == 'nan':
                address = ''

            owner_name_parts = owner_name.split()
            if any(word.lower().startswith(first_name.lower()) for word in owner_name_parts):
                city, state, zip_code = geocode.geocode_address(address)
                print(f"Match found for {name} with owner: {owner_name}, address: {address}")
                results.append({
                    "Full Name": name,
                    "Owner Name": owner_name,
                    "Parcel ID": parcel_id,
                    "Address": address,
                    "City": city,
                    "State": state,
                    "Zip": zip_code,
                })
                pipeline_db.add_match(name, owner_name, parcel_id, address, city, state, zip_code, posted_date)

    except Exception as e:
        # str(e) on a bare TimeoutException is empty - that's what's been
        # showing up as an uninformative "Message: " with no real content in
        # every prior run's logs. type(e).__name__ plus current_url/title at
        # the moment of failure is what actually tells us where this is
        # dying (never finding the search box at all vs. never getting a
        # second window to open vs. something else entirely).
        try:
            current_url = driver.current_url
            title = driver.title
        except Exception:
            current_url, title = "<driver unusable>", "<driver unusable>"
        _log(
            f"Error during search for {last_name}: {type(e).__name__}: {e} "
            f"(current_url={current_url!r}, title={title!r})"
        )

    driver.quit()

# Results are already cached in pipeline_db as they were found.
# Run DC.py after this to confirm matches and produce the final Excel file.
print("Final results cached:")
print(results)

# Delete the downloaded Excel file to avoid naming conflicts on next download
if os.path.exists(downloaded_excel_file):
    os.remove(downloaded_excel_file)
    print(f"Deleted the file: {downloaded_excel_file}")
else:
    print(f"File not found, could not delete: {downloaded_excel_file}")
