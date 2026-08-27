# pricer.py — build spec

For: Graham County, Arizona

This file is not auto-generated — too much of it depends on how this
county's specific property-assessor website is built. Use this doc (plus
your own look at the live site in dev tools) to write it from scratch.

## Where this fits in the pipeline

`master.py` runs four scripts in order: `obi.py` → `$.py` → `DC.py` →
`pricer.py`. By the time this script runs, `DC.py` has already confirmed
which candidate matches are real and written them to a dated Excel file
(or one file per posted date, in weekly mode). This script's job: for each
confirmed match, look up that property's assessed value on the assessor
site, and write it into the Excel file as a new "Property Value" column.

This is the last script in the pipeline — nothing downstream depends on
its output except a human reading the resulting Excel file (or the
dashboard, which reads the same files).

## File name and location

Must be saved as `pricer.py` in this same folder, alongside
`pipeline_db.py`, `geocode.py`, `$.py`, `DC.py`, `master.py`.

## What's already built for you — don't reinvent these

- `DC.py` writes a small JSON manifest so this script knows exactly which
  file(s) to open, instead of guessing from today's date (important in
  weekly mode, where `DC.py` can write up to 7 files, one per posted date):
  `.last_dc_run.json` in this same folder, shaped like
  `{"mode": "daily"|"weekly", "files": ["path/to/2026-07-27.xlsx", ...]}`.
- Each Excel file has, at minimum, "Full Name" and "Parcel ID" columns —
  one row per confirmed match.
- `pipeline_db.get_address_by_parcel_id(parcel_id)` → returns
  `(address, city, state, zip_code)`, already cached back when `$.py`
  found this match. Use this instead of re-scraping the address — city,
  state, and zip in particular were already resolved by
  `geocode.geocode_address()` inside `$.py` and shouldn't need
  re-deriving here.

## What this script needs to do

1. Read `.last_dc_run.json` to get the list of file(s) to process.
2. For each row in each file: look up `parcel_id`'s address via
   `pipeline_db.get_address_by_parcel_id()`, then search the assessor site
   by parcel id and read off the assessed property value.
3. Add (or overwrite) a "Property Value" column on that row.
4. Re-save the file in place (`df.to_excel(path, index=False)`).
5. If a parcel id has no cached address (shouldn't normally happen), or
   the lookup fails outright, write something explicit like
   `"No address on file"` or `"Not found"` in that cell rather than
   leaving it blank or letting the whole run crash.

## General guardrails

- A fresh browser window per parcel lookup is fine — simpler than reusing
  one session across every row, and a crashed/stuck browser only costs one
  lookup instead of the whole file.
- Add a small randomized pause between lookups (a couple seconds) — looks
  less like a bot hammering the site back-to-back.
- Wrap each lookup in try/except — one failed parcel shouldn't stop the
  rest of the file from being priced.
- You likely don't need a separate city/zip-filling pass — that's already
  handled upstream by `$.py`'s call into `geocode.py`. Keep it in mind
  only if those cached values ever come back empty for some reason.
- Don't guess selectors from Salt Lake County's old version of this file —
  every assessor site's DOM is different. Look at the real thing.
