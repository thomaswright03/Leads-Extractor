import json
import os
import random
import sys
import time

import pandas as pd
import requests

import pipeline_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Written by DC.py at the end of its run - lists exactly the file(s) it just
# produced (one "today" file in daily mode, or one per posted date in weekly
# mode), so this script doesn't have to guess from today's date.
LAST_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_dc_run.json")

API_BASE = "https://www.asr.pima.gov/AssessorSiteData/api/get"


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


def _request_with_retries(method, url, **kwargs):
    """A burst of ~15-20 requests with no pacing at all (confirmed live,
    during development) got this API rate-limited hard enough that even
    plain unrelated GETs to it started connection-timing-out for roughly a
    minute afterward - not a clean 429, a WAF-level drop. A real run's
    request volume is nowhere near what triggered that, especially with
    the pacing already between parcels below, but retrying with backoff
    here means one transient block doesn't take out the whole file."""
    last_exc = None
    for attempt in range(4):
        try:
            resp = method(url, timeout=30, **kwargs)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(5 * (attempt + 1))
    raise last_exc


def get_current_taxyear():
    resp = _request_with_retries(requests.get, f"{API_BASE}/dynamicdetails/", params={"function": "GetPublicWebYear"})
    return int(resp.json()[0]["PublicWebYear"])


def get_property_value(parcel_id, taxyear):
    resp = _request_with_retries(
        requests.post, f"{API_BASE}/parceldetails/", json={"parcel": parcel_id, "taxyear": taxyear},
    )
    data = resp.json()
    if not isinstance(data, dict):
        # The API returns the bare JSON string "invalid" instead of an
        # object for a parcel number that doesn't exist (confirmed live).
        raise ValueError(f"Account {parcel_id} not found on assessor site.")

    valuations = data.get("NoticedValuationData") or []
    match = next((v for v in valuations if v.get("TaxYear") == taxyear), None) or (
        valuations[0] if valuations else None
    )
    total_fcv = match.get("TotalFCV") if match else None
    if total_fcv is None:
        raise ValueError(f"Account {parcel_id}: value column never populated.")
    return f"${total_fcv:,.0f}"


if __name__ == "__main__":
    mode = pipeline_db.resolve_mode(sys.argv)
    last_run_mode, matches_paths = get_last_run()

    if not matches_paths:
        print("No DC.py output found to price. Run DC.py first.")
        raise SystemExit(0)

    if last_run_mode and last_run_mode != mode:
        print(f"Note: DC.py's last run was '{last_run_mode}' mode - pricing those {len(matches_paths)} file(s) regardless of the mode picked here.")

    taxyear = get_current_taxyear()

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
            try:
                value = get_property_value(parcel_id, taxyear)
                print(f"  -> {value}")
                property_values.append(value)
            except Exception as e:
                print(f"  -> Failed to get property value: {e}")
                property_values.append("Not found")

            # A lightweight JSON call, not a full browser page load like
            # every other county's pricer.py - still a small courteous
            # pause between requests rather than hammering the API in a
            # tight loop, just a shorter one given the far smaller load.
            time.sleep(random.uniform(0.5, 1.5))

        df["Property Value"] = property_values
        df.to_excel(matches_path, index=False)
        print(f"Updated {matches_path} with {len(property_values)} property value(s).")
