import http.server
import json
import os
import re
import webbrowser
from datetime import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")
PORT = 8765

# Matches DC.py's get_output_path() naming: "2026-07-21.xlsx" or, on a second
# run the same day, "2026-07-21_14-30-05.xlsx".
DATED_XLSX_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_\d{2}-\d{2}-\d{2})?\.xlsx$")


# Pima's parcel search site (asr.pima.gov) is an Angular SPA that keeps the
# currently-viewed parcel in sessionStorage, not the URL - confirmed live:
# every parcel detail page's actual address bar URL is the same bare
# /parcel-details, with no query param or path segment for the parcel
# number. Same practical problem as Greenlee/Graham's session-scoped GUID
# links (no link that works for someone else, or in a fresh tab), just
# implemented differently - so unlike a county with a real deep link, this
# just points at the bare homepage; the parcel/owner still needs to be
# typed into the search box there by hand.
PARCEL_URL_BASE = 'https://www.asr.pima.gov/'


def assessor_url(parcel_id):
    return PARCEL_URL_BASE


def format_date_added(date_str, fmt="%Y-%m-%d %H:%M:%S"):
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, fmt).strftime("%b %d, %Y")
    except ValueError:
        return date_str


def _clean_cell(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def discover_dated_excel_files():
    """Every DC.py export sitting in the project folder, named YYYY-MM-DD[_HH-MM-SS].xlsx."""
    found = []
    for name in os.listdir(SCRIPT_DIR):
        m = DATED_XLSX_PATTERN.match(name)
        if m:
            found.append((m.group(1), os.path.join(SCRIPT_DIR, name)))
    # Sort by actual mtime, not filename - DC.py can regenerate a fresh
    # "{date}.xlsx" (no suffix) after an earlier same-day conflict already
    # pushed an older run out to "{date}_HH-MM-SS.xlsx", so the bare
    # filename isn't reliably the oldest one on disk. A plain filename sort
    # would then let that older, stale suffixed file overwrite the newer
    # one's rows in load_confirmed_leads_from_excel()'s dedup below.
    found.sort(key=lambda pair: os.path.getmtime(pair[1]))
    return found


def load_confirmed_leads_from_excel():
    """Confirmed leads are the durable historical record - pipeline_cache.db's
    matches table gets wiped at the start of every $.py run, so anything
    from a previous day only survives in that day's dated export. Read every
    such file in the folder and merge them into one list, keyed by
    (full name, parcel id) so a lead re-confirmed on a later date overwrites
    the earlier entry instead of duplicating it."""
    leads_by_key = {}
    for date_str, path in discover_dated_excel_files():
        try:
            df = pd.read_excel(path)
        except Exception as e:
            print(f"Skipping {os.path.basename(path)}: {e}")
            continue

        date_display = format_date_added(date_str, fmt="%Y-%m-%d")
        for _, row in df.iterrows():
            full_name = _clean_cell(row.get("Full Name", ""))
            if not full_name:
                continue
            parcel_id = _clean_cell(row.get("Parcel ID", ""))
            matched_keywords_raw = _clean_cell(row.get("Matched Keywords", ""))

            leads_by_key[(full_name, parcel_id)] = {
                "fullName": full_name,
                "ownerName": _clean_cell(row.get("Owner Name", "")),
                "parcelId": parcel_id,
                "assessorUrl": assessor_url(parcel_id),
                "confirmed": True,
                "matchedKeywords": [k.strip() for k in matched_keywords_raw.split(",") if k.strip()],
                "obituaryUrl": _clean_cell(row.get("Obituary URL", "")),
                "propertyValue": _clean_cell(row.get("Property Value", "")),
                "address": _clean_cell(row.get("Address", "")),
                "city": _clean_cell(row.get("City", "")),
                "state": _clean_cell(row.get("State", "")),
                "zip": _clean_cell(row.get("Zip", "")),
                "dateAdded": date_display,
                "dateAddedRaw": date_str,
                "sourceFile": os.path.basename(path),
            }
    return list(leads_by_key.values())


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Probate Property Leads</title>
<style>
  :root {
    --navy: #1f2a3d;
    --navy-light: #2e3d57;
    /* Text color for headings/links/numbers - distinct from --navy because
       --navy also backs the always-dark header bar. Overriding --navy alone
       for dark mode would make the header's own white text unreadable. */
    --heading: #1f2a3d;
    --gold: #b8902e;
    --gold-light: #e9c874;
    --cream: #faf6ee;
    --card: #ffffff;
    --ink: #26303f;
    --muted: #6b7688;
    --border: #e7e1d3;
    --confirmed-bg: #eef6ee;
    --confirmed-ink: #2f6b3a;
    --review-bg: #fbf3e4;
    --review-ink: #93691e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--cream);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  header.top {
    background: linear-gradient(135deg, var(--navy), var(--navy-light));
    color: #fff;
    padding: 36px 32px 44px;
  }
  .top-inner {
    max-width: 1180px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 16px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .brand .mark {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, var(--gold-light), var(--gold) 70%);
    flex-shrink: 0;
    box-shadow: 0 0 0 4px rgba(255,255,255,0.08);
  }
  .brand h1 {
    margin: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 26px;
    font-weight: 600;
    letter-spacing: 0.2px;
  }
  .brand p {
    margin: 2px 0 0;
    font-size: 13px;
    color: rgba(255,255,255,0.7);
  }
  .generated {
    font-size: 12px;
    color: rgba(255,255,255,0.55);
    text-align: right;
  }

  main {
    max-width: 1180px;
    margin: -26px auto 60px;
    padding: 0 32px;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    box-shadow: 0 8px 20px rgba(31,42,61,0.08);
  }
  .stat-card .num {
    font-family: Georgia, serif;
    font-size: 30px;
    font-weight: 700;
    color: var(--heading);
    line-height: 1;
  }
  .stat-card .label {
    margin-top: 6px;
    font-size: 12.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .stat-card.confirmed .num { color: var(--confirmed-ink); }

  .panel {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 10px 24px rgba(31,42,61,0.06);
    overflow: hidden;
    margin-bottom: 24px;
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 18px 22px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .panel-head h2 {
    margin: 0;
    font-family: Georgia, serif;
    font-size: 18px;
    color: var(--heading);
  }
  .panel-head .sub {
    font-size: 12.5px;
    color: var(--muted);
    margin-top: 2px;
  }
  .controls {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .search-box {
    position: relative;
  }
  .search-box input {
    padding: 9px 14px 9px 34px;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 13.5px;
    width: 240px;
    background: var(--cream);
    color: var(--ink);
  }
  .search-box::before {
    content: "";
    position: absolute;
    left: 13px;
    top: 50%;
    width: 10px;
    height: 10px;
    border: 2px solid var(--muted);
    border-radius: 50%;
    transform: translateY(-60%);
  }
  .search-box::after {
    content: "";
    position: absolute;
    left: 21px;
    top: 62%;
    width: 6px;
    height: 2px;
    background: var(--muted);
    transform: rotate(45deg);
  }
  button.ghost {
    border: 1px solid var(--border);
    background: var(--cream);
    color: var(--heading);
    padding: 8px 14px;
    border-radius: 999px;
    font-size: 13px;
    cursor: pointer;
  }
  button.ghost:hover { background: #f2ecdd; }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left;
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    padding: 10px 22px;
    border-bottom: 1px solid var(--border);
    background: #fbf9f4;
  }
  thead th[data-key] {
    cursor: pointer;
    user-select: none;
    position: relative;
    padding-right: 26px;
  }
  thead th[data-key]:hover { color: var(--heading, var(--navy)); }
  thead th[data-key]::after {
    content: "\\2195";
    position: absolute;
    right: 9px;
    opacity: 0.35;
    font-size: 11px;
  }
  thead th[data-key].sort-asc::after { content: "\\2191"; opacity: 1; color: var(--gold); }
  thead th[data-key].sort-desc::after { content: "\\2193"; opacity: 1; color: var(--gold); }
  tbody td {
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
    vertical-align: top;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: #fbf9f2; }
  .name-cell .deceased { font-weight: 600; color: var(--heading); }
  .name-cell .owner { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
  .chip {
    display: inline-block;
    background: var(--gold-light);
    color: #6b4f0a;
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11.5px;
    margin: 1px 3px 1px 0;
    font-weight: 600;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
  }
  .badge.confirmed { background: var(--confirmed-bg); color: var(--confirmed-ink); }
  .badge.review { background: var(--review-bg); color: var(--review-ink); }
  a.link {
    color: var(--heading);
    text-decoration: none;
    font-size: 13px;
    border-bottom: 1px solid var(--gold);
    padding-bottom: 1px;
  }
  a.link:hover { color: var(--gold); }
  .links-cell { display: flex; flex-direction: column; gap: 4px; }
  .mailed-cell {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .mailed-cell input[type="checkbox"] {
    width: 17px;
    height: 17px;
    accent-color: var(--confirmed-ink);
    cursor: pointer;
  }
  .mailed-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
  }
  .mailed-label.yes { color: var(--confirmed-ink); }
  .empty {
    padding: 40px 22px;
    text-align: center;
    color: var(--muted);
    font-size: 14px;
  }

  footer {
    max-width: 1180px;
    margin: 0 auto 40px;
    padding: 0 32px;
    color: var(--muted);
    font-size: 12px;
    text-align: center;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --cream: #14181f;
      --card: #1c212b;
      --ink: #e7e9ee;
      --muted: #93a0b4;
      --border: #2a3140;
      --heading: #dbe2ee;
      --confirmed-bg: #17281c;
      --confirmed-ink: #7bc98a;
      --review-bg: #2b220f;
      --review-ink: #e0b563;
    }
    thead th { background: #191e27; }
    tbody tr:hover { background: #202634; }
    .search-box input { background: #14181f; color: var(--ink); }
    button.ghost { background: #14181f; }
    button.ghost:hover { background: #232a38; }
  }

  @media print {
    header.top { background: var(--navy) !important; -webkit-print-color-adjust: exact; }
    .controls { display: none; }
    .stats { grid-template-columns: repeat(2, 1fr); }
  }

  @media (max-width: 720px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    thead { display: none; }
    tbody td { display: block; padding: 6px 18px; border-bottom: none; }
    tbody tr { border-bottom: 1px solid var(--border); padding: 10px 0; display: block; }
  }

  .login-screen {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--navy), var(--navy-light));
    padding: 24px;
  }
  .login-card {
    background: var(--card);
    border-radius: 16px;
    padding: 36px 34px;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.3);
  }
  .login-card .mark {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, var(--gold-light), var(--gold) 70%);
    margin: 0 auto 16px;
  }
  .login-card h1 {
    margin: 0 0 4px;
    font-family: Georgia, serif;
    font-size: 21px;
    color: var(--heading, var(--navy));
    text-align: center;
  }
  .login-card p.sub {
    margin: 0 0 24px;
    font-size: 13px;
    color: var(--muted);
    text-align: center;
  }
  .login-card label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
    margin-top: 16px;
  }
  .login-card input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    background: var(--cream);
    color: var(--ink);
  }
  .login-card button {
    width: 100%;
    margin-top: 22px;
    padding: 11px;
    border: none;
    border-radius: 999px;
    background: var(--navy);
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .login-card button:hover { background: var(--navy-light); }
  .login-error {
    display: none;
    margin-top: 14px;
    font-size: 12.5px;
    color: #b3261e;
    text-align: center;
  }
  .login-error.show { display: block; }
</style>
</head>
<body>

<div class="login-screen" id="login-screen">
  <form class="login-card" id="login-form">
    <div class="mark"></div>
    <h1>Probate Property Leads</h1>
    <p class="sub">Sign in to view today&rsquo;s report</p>
    <label for="login-email">Email</label>
    <input type="email" id="login-email" autocomplete="username" required>
    <label for="login-password">Password</label>
    <input type="password" id="login-password" autocomplete="current-password" required>
    <button type="submit">Sign In</button>
    <div class="login-error" id="login-error">Incorrect email or password.</div>
  </form>
</div>

<div id="app-content" style="display:none;">

<header class="top">
  <div class="top-inner">
    <div class="brand">
      <div class="mark"></div>
      <div>
        <h1>Probate Property Leads</h1>
        <p>Pima County &middot; deceased-owner match report</p>
      </div>
    </div>
    <div class="generated">Generated __GENERATED_AT__</div>
  </div>
</header>

<main>
  <div class="stats">
    <div class="stat-card confirmed">
      <div class="num">__CONFIRMED_COUNT__</div>
      <div class="label">Real Leads Generated</div>
    </div>
    <div class="stat-card">
      <div class="num">__LEAD_PURSUED_RATE__</div>
      <div class="label">Lead Pursued Rate</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div>
        <h2>Confirmed Leads</h2>
        <div class="sub">Property owner name corroborated by the deceased&rsquo;s obituary</div>
      </div>
      <div class="controls">
        <div class="search-box"><input id="search-confirmed" type="text" placeholder="Search name, owner, parcel&hellip;"></div>
        <button class="ghost" id="export-confirmed">Export CSV</button>
        <button class="ghost" onclick="window.print()">Print</button>
      </div>
    </div>
    <table id="confirmed-table">
      <thead>
        <tr>
          <th data-key="fullName">Deceased / Owner Record</th>
          <th data-key="parcelId">Parcel</th>
          <th data-key="address">Address</th>
          <th data-key="city">City</th>
          <th data-key="state">State</th>
          <th data-key="zip">Zip</th>
          <th data-key="propertyValue">Property Value</th>
          <th data-key="dateAdded">Date Added</th>
          <th data-key="mailedIn">Mailed In</th>
          <th data-key="dateMailedIn">Date Mailed In</th>
        </tr>
      </thead>
      <tbody id="confirmed-body"></tbody>
    </table>
    <div class="empty" id="confirmed-empty" style="display:none;">No confirmed leads yet &mdash; run the pipeline to populate this list.</div>
  </div>
</main>

<footer>
  Generated locally from pipeline_cache.db &middot; parcel and obituary links open the original source for verification before contacting anyone.
</footer>

<script id="lead-data" type="application/json">__LEAD_DATA_JSON__</script>
<script>
  const leadData = JSON.parse(document.getElementById('lead-data').textContent);
  const confirmed = leadData.confirmed;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function linkEl(href, label) {
    const a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'link';
    a.textContent = label;
    return a;
  }

  // "Mailed In" tracking has nowhere server-side to live (this is a static
  // file with no backend), so it's stored in this browser's localStorage,
  // keyed by parcel id (falling back to name+owner for leads with no
  // parcel). It survives reloads and future regenerations of this report,
  // but only in this browser - it won't follow the file elsewhere.
  const MAILED_KEY_PREFIX = 'probateLeadsMailed:';

  function mailedKey(lead) {
    return MAILED_KEY_PREFIX + (lead.parcelId || `${lead.fullName}|${lead.ownerName}`);
  }

  function getMailedState(lead) {
    try {
      const raw = localStorage.getItem(mailedKey(lead));
      if (!raw) return { mailed: false, date: '' };
      return JSON.parse(raw);
    } catch (e) {
      return { mailed: false, date: '' };
    }
  }

  function setMailedState(lead, mailed) {
    const now = Date.now();
    const date = mailed
      ? new Date(now).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' })
      : '';
    localStorage.setItem(mailedKey(lead), JSON.stringify({ mailed, date, timestamp: mailed ? now : 0 }));
    return date;
  }

  function renderRow(lead) {
    const tr = document.createElement('tr');

    const nameCell = el('td', 'name-cell');
    nameCell.appendChild(el('div', 'deceased', lead.fullName));
    nameCell.appendChild(el('div', 'owner', lead.ownerName));
    tr.appendChild(nameCell);

    const parcelCell = el('td');
    if (lead.assessorUrl) {
      parcelCell.appendChild(linkEl(lead.assessorUrl, lead.parcelId || 'View parcel'));
    } else {
      parcelCell.appendChild(el('span', null, lead.parcelId || '—'));
    }
    tr.appendChild(parcelCell);

    tr.appendChild(el('td', null, lead.address || '—'));
    tr.appendChild(el('td', null, lead.city || '—'));
    tr.appendChild(el('td', null, lead.state || '—'));
    tr.appendChild(el('td', null, lead.zip || '—'));
    tr.appendChild(el('td', null, lead.propertyValue || '—'));
    tr.appendChild(el('td', null, lead.dateAdded || '—'));

    const mailedState = getMailedState(lead);
    const mailedCell = el('td', 'mailed-cell');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = mailedState.mailed;
    const mailedLabel = el('span', 'mailed-label' + (mailedState.mailed ? ' yes' : ''), mailedState.mailed ? 'Yes' : 'No');
    const dateMailedCell = el('td', null, mailedState.date || '—');

    checkbox.addEventListener('change', () => {
      const newDate = setMailedState(lead, checkbox.checked);
      mailedLabel.textContent = checkbox.checked ? 'Yes' : 'No';
      mailedLabel.className = 'mailed-label' + (checkbox.checked ? ' yes' : '');
      dateMailedCell.textContent = newDate || '—';
    });

    mailedCell.appendChild(checkbox);
    mailedCell.appendChild(mailedLabel);
    tr.appendChild(mailedCell);
    tr.appendChild(dateMailedCell);

    return tr;
  }

  function paint(list, bodyId, emptyId) {
    const body = document.getElementById(bodyId);
    const empty = document.getElementById(emptyId);
    body.innerHTML = '';
    if (!list.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    list.forEach(l => body.appendChild(renderRow(l)));
  }

  function matches(lead, q) {
    if (!q) return true;
    q = q.toLowerCase();
    return lead.fullName.toLowerCase().includes(q) ||
           lead.ownerName.toLowerCase().includes(q) ||
           lead.parcelId.toLowerCase().includes(q) ||
           lead.address.toLowerCase().includes(q) ||
           lead.city.toLowerCase().includes(q) ||
           lead.zip.toLowerCase().includes(q);
  }

  // Column sorting. Most columns sort on a plain lead field, but "Mailed In"
  // and "Date Mailed In" live in localStorage rather than the lead object,
  // so they're computed fresh at sort time.
  function sortValue(lead, key) {
    switch (key) {
      case 'mailedIn':
        return getMailedState(lead).mailed ? 1 : 0;
      case 'dateMailedIn':
        return getMailedState(lead).timestamp || 0;
      case 'dateAdded':
        return lead.dateAddedRaw || '';
      case 'propertyValue': {
        const num = parseFloat((lead.propertyValue || '').replace(/[^0-9.]/g, ''));
        return isNaN(num) ? -1 : num;
      }
      default:
        return (lead[key] || '').toString().toLowerCase();
    }
  }

  const confirmedState = { query: '', sortKey: null, sortDir: 'asc' };

  function applyState(list, state) {
    let result = state.query ? list.filter(l => matches(l, state.query)) : list.slice();
    if (state.sortKey) {
      result.sort((a, b) => {
        const va = sortValue(a, state.sortKey);
        const vb = sortValue(b, state.sortKey);
        if (va < vb) return state.sortDir === 'asc' ? -1 : 1;
        if (va > vb) return state.sortDir === 'asc' ? 1 : -1;
        return 0;
      });
    }
    return result;
  }

  function updateSortIndicators(tableId, state) {
    document.querySelectorAll(`#${tableId} thead th[data-key]`).forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.key === state.sortKey) {
        th.classList.add(state.sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
      }
    });
  }

  function repaintConfirmed() {
    paint(applyState(confirmed, confirmedState), 'confirmed-body', 'confirmed-empty');
    updateSortIndicators('confirmed-table', confirmedState);
  }

  function setupSortableHeaders(tableId, state, repaintFn) {
    document.querySelectorAll(`#${tableId} thead th[data-key]`).forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = key;
          state.sortDir = 'asc';
        }
        repaintFn();
      });
    });
  }

  setupSortableHeaders('confirmed-table', confirmedState, repaintConfirmed);

  repaintConfirmed();

  document.getElementById('search-confirmed').addEventListener('input', e => {
    confirmedState.query = e.target.value;
    repaintConfirmed();
  });

  document.getElementById('export-confirmed').addEventListener('click', () => {
    const rows = [['Deceased', 'Owner Record', 'Parcel ID', 'Address', 'City', 'State', 'Zip', 'Property Value', 'Matched Keywords', 'Obituary URL', 'Assessor URL', 'Date Added', 'Mailed In', 'Date Mailed In']];
    confirmed.forEach(l => {
      const mailedState = getMailedState(l);
      rows.push([
        l.fullName, l.ownerName, l.parcelId, l.address, l.city, l.state, l.zip, l.propertyValue, l.matchedKeywords.join('; '), l.obituaryUrl, l.assessorUrl, l.dateAdded,
        mailedState.mailed ? 'Yes' : 'No', mailedState.date
      ]);
    });
    const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\\n');
    const blob = new Blob([csv], {type: 'text/csv'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'confirmed_leads.csv';
    a.click();
  });
</script>

</div>

<script>
  // NOTE: this is a client-side gate on a static local file, not real
  // access control - the credentials below are visible to anyone who views
  // the page source. It just keeps the report from being open by default.
  const VALID_EMAIL = "thomasjr@leads.extractor";
  const VALID_PASSWORD = "239184";
  const SESSION_KEY = "probateLeadsAuthed";

  const loginScreen = document.getElementById('login-screen');
  const appContent = document.getElementById('app-content');
  const loginForm = document.getElementById('login-form');
  const loginError = document.getElementById('login-error');

  function unlock() {
    loginScreen.style.display = 'none';
    appContent.style.display = 'block';
  }

  if (sessionStorage.getItem(SESSION_KEY) === 'true') {
    unlock();
  }

  loginForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim().toLowerCase();
    const password = document.getElementById('login-password').value;
    if (email === VALID_EMAIL && password === VALID_PASSWORD) {
      sessionStorage.setItem(SESSION_KEY, 'true');
      loginError.classList.remove('show');
      unlock();
    } else {
      loginError.classList.add('show');
    }
  });
</script>

</body>
</html>
"""


def build_dashboard_html():
    """Re-reads the dated xlsx files and rebuilds the page fresh - called on
    every request so a browser refresh always reflects the latest exports,
    with no need to re-run this script."""
    confirmed_leads = load_confirmed_leads_from_excel()
    confirmed_count = len(confirmed_leads)

    # Lead Pursued Rate (% of leads generated that got sent a letter) isn't
    # tracked yet - left blank until that's wired up.
    lead_pursued_rate = ""

    lead_data = {"confirmed": confirmed_leads}

    return (TEMPLATE
        .replace("__GENERATED_AT__", datetime.now().strftime("%B %d, %Y at %-I:%M %p"))
        .replace("__CONFIRMED_COUNT__", str(confirmed_count))
        .replace("__LEAD_PURSUED_RATE__", lead_pursued_rate)
        .replace("__LEAD_DATA_JSON__", json.dumps(lead_data))
    )


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = build_dashboard_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default access log (one line per request, including favicon
        # requests) is noisy for a single-user local tool - just note refreshes.
        print(f"Dashboard refreshed at {datetime.now().strftime('%H:%M:%S')}")


def main():
    # Also write a static snapshot to disk, for reference / opening without
    # the server running - but the server below is the primary way to view
    # this now, since it rebuilds from the xlsx files on every request.
    with open(OUTPUT_PATH, "w") as f:
        f.write(build_dashboard_html())

    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), DashboardRequestHandler)
    except OSError as e:
        print(f"Couldn't start the dashboard server on port {PORT}: {e}")
        print("Is dashboard.py already running in another terminal? If so, just refresh that browser tab.")
        raise SystemExit(1)

    url = f"http://127.0.0.1:{PORT}/"
    print(f"Dashboard live at {url} - refresh the page any time to see the latest data.")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
        server.shutdown()


if __name__ == "__main__":
    main()
