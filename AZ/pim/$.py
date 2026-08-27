import random
import re
import sys
import time

import requests

import pipeline_db

# Pima County's assessor site (asr.pima.gov) is a modern Angular SPA backed
# by a fully open, unauthenticated JSON API at asr.pima.gov/AssessorSiteData
# /api/get/... - confirmed live via browser network-tab inspection and a
# bare `curl` with no cookies or special headers beyond Content-Type. Every
# other county here needs Selenium because its assessor site is
# server-rendered HTML with real page loads to wait on; Pima's isn't, so
# this talks to the JSON API directly instead of driving a browser through
# it - simpler and far more reliable than DOM-scraping the same data this
# API already hands back cleanly.
API_BASE = "https://www.asr.pima.gov/AssessorSiteData/api/get"

ZIP_RE = re.compile(r"\b(\d{5})\b")


def _request_with_retries(method, url, **kwargs):
    """A burst of ~15-20 requests with no pacing at all (confirmed live,
    during development) got this API rate-limited hard enough that even
    plain unrelated GETs to it started connection-timing-out for roughly a
    minute afterward - not a clean 429, a WAF-level drop. A real run's
    request volume (a handful of names, a couple calls each) is nowhere
    near what triggered that, but retrying with backoff here means one
    transient block doesn't take out the whole run."""
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


def search_owner(last_name, taxyear):
    """Every owner-record row whose Owner field contains last_name as a
    substring (confirmed live: a plain "contains" match, same as most
    other counties here, not last-name-only) - capped at 200 rows
    server-side regardless of how many real matches exist (confirmed live:
    the site's own UI even warns "will only show a max of 200 results per
    section", and there's no offset/pagination param exposed to get past
    it). A real obituary surname essentially never hits that cap in
    practice - only pathologically common ones like "Smith" would."""
    resp = _request_with_retries(
        requests.post, f"{API_BASE}/PropertyOwnerResults/", json={"search": last_name, "taxyear": taxyear},
    )
    data = resp.json()
    return data if isinstance(data, list) else []


def get_parcel_details(parcel_id, taxyear):
    """None if the parcel doesn't exist - the API returns the bare JSON
    string "invalid" instead of an object in that case (confirmed live)."""
    resp = _request_with_retries(
        requests.post, f"{API_BASE}/parceldetails/", json={"parcel": parcel_id, "taxyear": taxyear},
    )
    data = resp.json()
    return data if isinstance(data, dict) else None


def parse_mailing_address(mailing):
    """The owner's mailing address, not the property's situs address - the
    right one to mail a probate letter to, since heirs often don't live at
    the property itself. The API renders this as a flat print address
    block across ParcelOwner/Mail2/Mail3/Mail4/Mail5 - how many of those
    lines hold owner-name/"ATTN:" text before the real street+city lines
    varies record to record (confirmed live: a short owner name puts
    street+city in Mail2/Mail3, a long one with an attention line pushes
    them out to Mail4/Mail5), but the street and city/state are always
    whichever two non-blank lines among Mail2..Mail5 come last - so scan
    for those instead of trusting fixed slots. ParcelOwner itself is never
    part of this scan; it's always name, never address."""
    lines = [(mailing.get(k) or "").strip() for k in ("Mail2", "Mail3", "Mail4", "Mail5")]
    lines = [line for line in lines if line]
    street = lines[-2] if len(lines) >= 2 else (lines[-1] if lines else "")
    city_state = lines[-1] if len(lines) >= 2 else ""

    city, state = "", ""
    if city_state:
        if "," in city_state:
            city, state = (p.strip() for p in city_state.rsplit(",", 1))
        else:
            parts = city_state.split()
            if len(parts) >= 2:
                state = parts[-1]
                city = " ".join(parts[:-1])

    zip_code = (mailing.get("Zip") or "").strip()
    zip_match = ZIP_RE.search(zip_code)
    zip_code = zip_match.group(1) if zip_match else zip_code

    return street, city, state, zip_code


if __name__ == "__main__":
    pipeline_db.resolve_mode(sys.argv)  # not used here, but keeps argv[1] consistent with the rest of the pipeline

    taxyear = get_current_taxyear()

    names_with_dates = pipeline_db.get_obituary_names_with_dates()
    pipeline_db.clear_matches()

    print("Names to search:")
    for name, posted_date in names_with_dates:
        print(f"{name} ({posted_date})" if posted_date else name)

    for name, posted_date in names_with_dates:
        time.sleep(random.uniform(0.5, 1.5))
        try:
            last_name = extract_last_name(name)
            first_name = extract_first_name(name)
            if not last_name or not first_name:
                print(f"Skipping '{name}' - couldn't parse a first/last name.")
                continue

            print(f"Searching for last name: {last_name}")
            candidates = search_owner(last_name, taxyear)

            if not candidates:
                print(f"No results found for {name}. Moving on.")
                continue

            for row in candidates:
                owner_name = (row.get("Owner") or "").strip()
                parcel_id = (row.get("Parcel") or "").strip()
                if not owner_name or not parcel_id:
                    continue

                owner_words = owner_name.split()
                if not any(w.lower().startswith(first_name.lower()) for w in owner_words):
                    continue

                # A courteous pause before every parcel-detail lookup - a
                # tight loop with none of these is what triggered the rate
                # limit noted on _request_with_retries above.
                time.sleep(random.uniform(0.5, 1.5))
                details = get_parcel_details(parcel_id, taxyear)
                if not details:
                    print(f"  Couldn't load parcel details for {parcel_id}")
                    continue

                street, city, state, zip_code = parse_mailing_address(details.get("Mailing") or {})
                if not street:
                    print(f"  {parcel_id} ({owner_name}) has no mailing address on file - skipping.")
                    continue

                print(f"Match found for {name}: owner={owner_name}, parcel={parcel_id}, address={street}")
                pipeline_db.add_match(
                    name, owner_name, parcel_id,
                    street, city, state, zip_code, posted_date,
                )

        except Exception as e:
            print(f"Error during search for '{name}': {e}")

    print("Done.")
