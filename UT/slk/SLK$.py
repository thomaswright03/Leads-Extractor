import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
import time
import re
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os  # <-- Added for file deletion
from urllib.parse import urlparse, parse_qs

import pipeline_db
import geocode

# Path to the downloaded Excel file to process and later delete
downloaded_excel_file = os.path.expanduser('~/Downloads/pt1091601.xls')
download_dir = os.path.dirname(downloaded_excel_file)

# Load the names to search from the shared cache (populated by SLKobi copy.py)
names_with_dates = pipeline_db.get_obituary_names_with_dates()

# Start this run's match cache fresh
pipeline_db.clear_matches()

print("Names to search:")
for name, posted_date in names_with_dates:
    print(f"{name} ({posted_date})" if posted_date else name)

def extract_last_name(full_name):
    name_parts = full_name.strip().split()
    if name_parts[-1].lower() in ['jr.', 'sr.']:
        return name_parts[-2]
    name_parts = re.sub(r'\(.*\)$', '', ' '.join(name_parts)).split()
    return name_parts[-1]

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
    name_parts = full_name.strip().split()
    if len(name_parts) > 1:
        return name_parts[0].capitalize()
    return ''

results = []

for index, (name, posted_date) in enumerate(names_with_dates):
    last_name = extract_last_name(name)
    first_name = extract_first_name(name)

    options = webdriver.ChromeOptions()
    # options.add_argument('--headless')  # Uncomment if you want headless
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })

    # Plain webdriver.Chrome() lets Selenium's own built-in driver manager
    # handle the chromedriver binary - switched from webdriver_manager's
    # ChromeDriverManager().install(), which started failing every single
    # search silently in CI (confirmed live: SLKobi.py in this same
    # workflow, which never used webdriver_manager, kept working fine).
    driver = webdriver.Chrome(options=options)

    try:
        driver.get("https://www.saltlakecounty.gov/assessor/")
        time.sleep(0.5)

        print(f"Searching for last name: {last_name}")

        search_box_owner = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "OwnerSearch"))
        )
        search_box_owner.clear()
        search_box_owner.send_keys(last_name)

        search_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, "/html/body/section[3]/div/div/main/section[1]/div[2]/div/form/button[1]"))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
        time.sleep(0.5)
        search_button.click()

        time.sleep(3)

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

        download_img = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "/html/body/section[4]/div/div/main/section/div/div[1]/div[2]/div/a/img"))
        )
        time.sleep(1)
        safe_click(driver, download_img)
        print("Clicked the download image button.")

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
        print(f"Error during search for {last_name}: {e}")

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
