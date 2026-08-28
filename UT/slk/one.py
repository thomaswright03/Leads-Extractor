import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ADDRESS_BOX_XPATH = "/html/body/section[3]/div/div/main/section[1]/div[2]/div[1]/form/p[2]/input"
AUTOCOMPLETE_XPATH = "/html/body/ul/li/div"

# A mix of addresses that failed to geocode before, plus one that worked,
# for comparison.
TEST_ADDRESSES = [
    "393 W PRINCETON DR",
    "7041 S CATALINA DR",
    "8947 S HEIGHTS DR",
    "1581 E ALTAIR CIR",
    "2024 E PRINCETON DR",
]

options = uc.ChromeOptions()
# Not eager - we need the page's autocomplete JS fully wired up before typing.
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
driver = uc.Chrome(options=options)

try:
    driver.get("https://www.saltlakecounty.gov/assessor/")

    address_box = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, ADDRESS_BOX_XPATH))
    )

    for address in TEST_ADDRESSES:
        address_box.clear()
        address_box.send_keys(address)

        # Hard wait for the autocomplete dropdown to populate.
        time.sleep(2)

        try:
            result_el = driver.find_element(By.XPATH, AUTOCOMPLETE_XPATH)
            print(f"{address!r} -> {result_el.text.strip()!r}")
        except Exception as e:
            print(f"{address!r} -> NOT FOUND ({e})")

finally:
    driver.quit()
