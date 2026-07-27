import json
import urllib.parse
import urllib.request

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def geocode_address(street_address):
    """Look up city/state/zip for a Salt Lake County street address via the
    free US Census geocoder. The assessor site only ever gives a bare street
    address (no city/zip), so this fills in the rest. State is always Utah
    since Salt Lake County is entirely within it - only city/zip actually
    depend on the lookup succeeding.
    """
    state = "UT"
    if not street_address:
        return "", state, ""

    query = f"{street_address}, Salt Lake County, UT"
    url = f"{CENSUS_GEOCODER_URL}?{urllib.parse.urlencode({'address': query, 'benchmark': 'Public_AR_Current', 'format': 'json'})}"

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return "", state, ""
        components = matches[0]["addressComponents"]
        city = components.get("city", "").title()
        zip_code = components.get("zip", "")
        return city, state, zip_code
    except Exception:
        return "", state, ""
