"""The /admin pipeline-health page: one row per county showing how its last
nightly run went - which step failed, how many Legacy.com lookups errored,
how many leads it confirmed, and when it last produced a lead - plus the
error lines each step printed. Reads only files the nightly workflow
commits next to each county's pipeline (.last_pipeline_run.json from
master.py, .last_dc_run.json from DC.py, and the dated .xlsx exports), so
it reflects whatever Render last deployed.

Kept out of dashboard.py, which is already very large; dashboard.py does
the admin-only gate and passes COUNTIES and its lead loader in."""

import html
import json
import os
from datetime import datetime, timedelta, timezone

ACTIONS_URL = "https://github.com/thomaswright03/Leads-Extractor/actions/workflows/nightly-extraction.yml"

# A county whose last recorded run is older than this missed at least one
# nightly run (the workflow runs once a day; GitHub often starts it hours late).
STALE_AFTER = timedelta(hours=36)

# Busy counties confirm leads most days, so two quiet weeks usually means
# something upstream broke rather than a genuinely empty fortnight.
NO_LEADS_WARN_AFTER = timedelta(days=14)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def county_health(county, county_dir, leads, now):
    """Everything the page shows for one county, plus a status of
    "ok" / "warn" / "fail" / "none" and the reasons behind it."""
    run = _read_json(os.path.join(county_dir, ".last_pipeline_run.json"))
    dc = _read_json(os.path.join(county_dir, ".last_dc_run.json")) or {}
    stats = dc.get("stats") or {}

    lead_dates = sorted({lead["dateAddedRaw"] for lead in leads if lead.get("dateAddedRaw")})
    last_lead_date = lead_dates[-1] if lead_dates else None
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    problems = []
    warnings = []
    if not last_lead_date:
        warnings.append("Has never produced a lead")
    elif last_lead_date < (now - NO_LEADS_WARN_AFTER).strftime("%Y-%m-%d"):
        warnings.append(f"No new leads since {last_lead_date}")
    finished_at = _parse_time((run or {}).get("finished_at"))
    if run is None:
        status = "none"
        warnings.append("No run recorded yet (written from the next nightly run on).")
    else:
        if not run.get("ok", True):
            problems.append(f"Stopped at {run.get('failed_step') or 'a step'}")
        attempted = stats.get("lookups_attempted") or 0
        failed = stats.get("lookups_failed") or 0
        if attempted and failed == attempted:
            problems.append(f"All {attempted} obituary lookups failed")
        elif failed:
            warnings.append(f"{failed} of {attempted} obituary lookups failed")
        noisy = [f"{step.get('script')} printed {step.get('error_count')} error line(s)"
                 for step in run.get("steps", []) if step.get("error_count") and step.get("returncode") == 0]
        warnings.extend(noisy)
        if finished_at and now - finished_at > STALE_AFTER:
            warnings.append("Missed the last nightly run")
        status = "fail" if problems else ("warn" if warnings else "ok")

    steps = []
    for step in (run or {}).get("steps", []):
        steps.append({
            "script": step.get("script", ""),
            "returncode": step.get("returncode"),
            "seconds": step.get("seconds"),
            "error_count": step.get("error_count", 0),
            "error_lines": step.get("error_lines", []),
        })

    return {
        "label": county["label"],
        "state": county["state"],
        "status": status,
        "problems": problems,
        "warnings": warnings,
        "finished_at": finished_at,
        "stats": stats,
        "steps": steps,
        "last_lead_date": last_lead_date,
        "leads_7d": sum(1 for lead in leads if (lead.get("dateAddedRaw") or "") >= week_ago),
        "leads_30d": sum(1 for lead in leads if (lead.get("dateAddedRaw") or "") >= month_ago),
        "leads_total": len(leads),
    }


def _fmt_time(value, now):
    if not value:
        return "never"
    hours = (now - value).total_seconds() / 3600
    ago = f"{int(hours)}h ago" if hours < 48 else f"{int(hours // 24)}d ago"
    return f"{value.strftime('%b %d, %H:%M')} UTC ({ago})"


def _fmt_lead_date(value, now):
    if not value:
        return "never"
    try:
        days = (now.date() - datetime.strptime(value, "%Y-%m-%d").date()).days
    except ValueError:
        return html.escape(value)
    return f"{value} ({'today' if days == 0 else f'{days}d ago'})"


STATUS_LABELS = {"ok": "Healthy", "warn": "Warning", "fail": "Failing", "none": "No data"}


def build_admin_html(counties, script_dir, load_leads, email):
    now = datetime.now(timezone.utc)
    rows = []
    for county in counties:
        county_dir = os.path.join(script_dir, county.get("path", county["folder"]))
        rows.append(county_health(county, county_dir, load_leads(county), now))
    order = {"fail": 0, "warn": 1, "none": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["status"]], r["label"]))

    counts = {key: sum(1 for r in rows if r["status"] == key) for key in order}
    summary = " &middot; ".join(
        f'<span class="pill {key}">{counts[key]} {STATUS_LABELS[key].lower()}</span>'
        for key in ("fail", "warn", "none", "ok") if counts[key]
    )

    cards = []
    for r in rows:
        stats = r["stats"]
        notes = "".join(f'<li class="problem">{html.escape(p)}</li>' for p in r["problems"])
        notes += "".join(f'<li class="warning">{html.escape(w)}</li>' for w in r["warnings"])
        step_rows = []
        for step in r["steps"]:
            failed = step["returncode"] not in (0, None)
            lines = "\n".join(step["error_lines"])
            details = (
                f'<details><summary>{step["error_count"]} error line(s)</summary><pre>{html.escape(lines)}</pre></details>'
                if step["error_lines"] else '<span class="muted">none</span>'
            )
            step_rows.append(
                f'<tr class="{"step-failed" if failed else ""}"><td><code>{html.escape(step["script"])}</code></td>'
                f'<td>{"exit " + str(step["returncode"]) if failed else "ok"}</td>'
                f'<td>{step["seconds"] if step["seconds"] is not None else "?"}s</td><td>{details}</td></tr>'
            )
        steps_table = (
            '<table class="steps"><thead><tr><th>Step</th><th>Result</th><th>Time</th><th>Errors</th></tr></thead>'
            f'<tbody>{"".join(step_rows)}</tbody></table>' if step_rows else ""
        )
        lookups = (
            f'{stats.get("lookups_failed", 0)} / {stats.get("lookups_attempted", 0)} failed'
            if "lookups_attempted" in stats else "&ndash;"
        )
        cards.append(f"""
<section class="card {r['status']}">
  <div class="card-head">
    <h2>{html.escape(r['label'])}, {html.escape(r['state'])}</h2>
    <span class="pill {r['status']}">{STATUS_LABELS[r['status']]}</span>
  </div>
  {f'<ul class="notes">{notes}</ul>' if notes else ''}
  <dl class="facts">
    <div><dt>Last run</dt><dd>{_fmt_time(r['finished_at'], now)}</dd></div>
    <div><dt>Candidates</dt><dd>{stats.get('candidates', '&ndash;')}</dd></div>
    <div><dt>Obituary lookups</dt><dd>{lookups}</dd></div>
    <div><dt>Confirmed (last run)</dt><dd>{stats.get('confirmed', '&ndash;')}</dd></div>
    <div><dt>Last lead</dt><dd>{_fmt_lead_date(r['last_lead_date'], now)}</dd></div>
    <div><dt>Leads 7d / 30d / all</dt><dd>{r['leads_7d']} / {r['leads_30d']} / {r['leads_total']}</dd></div>
  </dl>
  {steps_table}
</section>""")

    return ADMIN_HTML.replace("__SUMMARY__", summary).replace(
        "__CARDS__", "".join(cards)
    ).replace("__GENERATED_AT__", now.strftime("%b %d, %Y %H:%M UTC")).replace(
        "__ACTIONS_URL__", ACTIONS_URL
    ).replace("__USER_EMAIL__", html.escape(email or ""))


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin - Data Lead Sell</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Inter', -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; background: #faf7f1; color: #232c3b; }
  header.top { background: linear-gradient(135deg, #0a1424, #101d33 55%, #24354f); color: #fff; padding: 22px 32px; }
  header.top a { color: #ecce8a; font-size: 13.5px; text-decoration: none; margin-right: 14px; }
  header.top h1 { font-family: Georgia, "Iowan Old Style", Palatino, serif; font-size: 24px; margin: 8px 0 0; }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 30px 24px 80px; }
  .sub { color: #707b8d; font-size: 13.5px; margin: 0 0 20px; }
  .card { background: #fff; border: 1px solid #e9e2d2; border-left-width: 5px; border-radius: 14px; padding: 18px 22px; margin-bottom: 16px; }
  .card.fail { border-left-color: #b4372f; }
  .card.warn { border-left-color: #c9932b; }
  .card.ok { border-left-color: #2a6b3d; }
  .card.none { border-left-color: #a3abb8; }
  .card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .card h2 { font-size: 17px; margin: 0; }
  .pill { display: inline-block; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; }
  .pill.fail { background: #fbe4e2; color: #8e2a23; }
  .pill.warn { background: #fbf1de; color: #8f631a; }
  .pill.ok { background: #eaf6ee; color: #2a6b3d; }
  .pill.none { background: #eef0f3; color: #5b6475; }
  .notes { margin: 12px 0 0; padding-left: 18px; font-size: 13.5px; }
  .notes .problem { color: #8e2a23; font-weight: 600; }
  .notes .warning { color: #8f631a; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px 18px; margin: 14px 0 0; }
  .facts dt { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: #707b8d; }
  .facts dd { margin: 2px 0 0; font-size: 14px; font-variant-numeric: tabular-nums; }
  table.steps { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 13px; }
  table.steps th { text-align: left; font-weight: 600; color: #707b8d; border-bottom: 1px solid #e9e2d2; padding: 6px 8px; }
  table.steps td { border-bottom: 1px solid #f2ece0; padding: 6px 8px; vertical-align: top; }
  tr.step-failed td { background: #fdf2f1; }
  details summary { cursor: pointer; color: #7c5e1f; }
  pre { white-space: pre-wrap; word-break: break-word; font-size: 11.5px; background: #f7f4ee; border-radius: 8px; padding: 10px; max-height: 320px; overflow: auto; }
  .muted { color: #a3abb8; }
  @media (max-width: 600px) { header.top { padding: 18px 16px; } .wrap { padding: 20px 16px 60px; } .card { padding: 14px 16px; } }
</style>
</head>
<body>
<header class="top">
  <a href="/">&larr; Dashboard</a><a href="/stats">Stats</a><a href="/map">The Map</a><a href="__ACTIONS_URL__" target="_blank" rel="noopener">GitHub Actions runs</a>
  <h1>Pipeline health</h1>
</header>
<div class="wrap">
  <p class="sub">Each county's last nightly run. Generated __GENERATED_AT__ for __USER_EMAIL__. __SUMMARY__</p>
  __CARDS__
</div>
</body>
</html>
"""
