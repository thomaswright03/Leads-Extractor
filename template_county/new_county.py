"""Dead-simple wizard: answer some questions about a new county, get a
configured copy of every script in this folder dropped into <state>/<county>/.

Run it:  python3 new_county.py
Then open the URL it prints (it also opens your browser automatically).

Deliberately not fancy - plain unstyled HTML, no CSS, no JS framework, no
external packages (stdlib http.server only). You're the only user.

(Originally a tkinter GUI, but the system Python on macOS ships an ancient
Tk 8.5 that renders plain Label/Combobox widgets invisibly - a long-standing
Apple bug, not something fixable from this script. A local web page sidesteps
it entirely.)
"""
import html
import http.server
import os
import webbrowser
from urllib.parse import parse_qs

TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TEMPLATE_DIR)
PORT = 8811

STATES = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
    ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"),
    ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"), ("Idaho", "ID"),
    ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"), ("Kansas", "KS"),
    ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
    ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"), ("Mississippi", "MS"),
    ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"),
    ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"),
    ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"), ("Oklahoma", "OK"),
    ("Oregon", "OR"), ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"),
    ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"),
    ("Vermont", "VT"), ("Virginia", "VA"), ("Washington", "WA"), ("West Virginia", "WV"),
    ("Wisconsin", "WI"), ("Wyoming", "WY"),
]
STATE_ABBR_BY_NAME = dict(STATES)

# Every file in this folder that should be copied into the new county folder,
# with any __TOKEN__ placeholders swapped for the answers collected below.
# (Not copied: pipeline_cache.db - starts fresh per county; this script itself.)
FILES_TO_COPY = [
    "obi.py", "DC.py", "pricerdev.py",
    "geocode.py", "dashboard.py", "dashboard.html", "master.py", "pipeline_db.py",
    "$.md", "pricer.md",
]

# Every token except these gets substituted as a Python string literal (via
# repr(), so quotes/backslashes inside an XPath don't break the generated
# file). These sit in either dashboard.py's raw HTML template string or the
# plain-text $.md/pricer.md build docs, not a Python assignment, so they're
# substituted as plain text instead.
RAW_TEXT_TOKENS = {"COUNTY_LABEL", "STATE_NAME"}

# (token, label, default hint). default=None means "auto-filled from county/state,
# shown editable" rather than a fixed Salt Lake example value.
FIELD_GROUPS = [
    ("Obituary source  (obi.py)", [
        ("OBITUARY_URL",
         "Obituary browse URL (auto-filled from state/county - override if this "
         "county isn't on the standard legacy.com /local/state/county-county pattern, "
         "e.g. Louisiana parishes or Alaska boroughs)",
         None),
    ]),
    # $.py and pricer.py are deliberately NOT templated - both are too
    # tied to the specific assessor site's markup (owner/parcel search,
    # results layout, property-value/autocomplete lookups) to fill out
    # reliably from a form. Both get written by hand per county and dropped
    # into the generated folder before running master.py.
    ("Geocoding fallback  (geocode.py)", [
        ("COUNTY_QUERY_SUFFIX", "Geocode query suffix", None),
    ]),
    ("Dashboard  (dashboard.py)", [
        ("PARCEL_URL_BASE", "Parcel deep-link URL base",
         "https://apps.saltlakecounty.gov/assessor/new/valuationInfoExpanded.cfm?parcel_id="),
        ("COUNTY_LABEL", "Report title text", None),
    ]),
]

STEPS = ["start", "state", "county"] + [f"group{i}" for i in range(len(FIELD_GROUPS))] + ["done"]


def build_county(answers):
    """answers: dict with STATE_ABBR, COUNTY, COUNTY_ABBR, plus every token
    from FIELD_GROUPS. Writes the configured county folder to disk and
    returns its path. Raises FileExistsError if the folder is already there."""
    state_abbr = answers["STATE_ABBR"]
    county_abbr = answers["COUNTY_ABBR"].strip().lower()

    target_dir = os.path.join(PROJECT_ROOT, state_abbr, county_abbr)
    if os.path.exists(target_dir):
        raise FileExistsError(target_dir)
    os.makedirs(target_dir)

    token_values = {k: v for k, v in answers.items()
                     if k not in ("STATE_ABBR", "STATE_NAME", "COUNTY", "COUNTY_ABBR")}
    token_values["STATE_ABBR"] = state_abbr
    token_values["STATE_NAME"] = answers["STATE_NAME"]

    for filename in FILES_TO_COPY:
        src = os.path.join(TEMPLATE_DIR, filename)
        with open(src, "r") as f:
            text = f.read()
        for token, value in token_values.items():
            substitution = value if token in RAW_TEXT_TOKENS else repr(value)
            text = text.replace(f"__{token}__", substitution)
        with open(os.path.join(target_dir, filename), "w") as f:
            f.write(text)

    return target_dir


# ---- in-memory wizard state (single local user, one process) ----
class WizardState:
    def __init__(self):
        self.step_index = 0
        self.answers = {}

    @property
    def step(self):
        return STEPS[self.step_index]


wizard = WizardState()

PAGE_WRAPPER = """<!doctype html>
<html>
<head>
<title>New County Setup</title>
<style>body {{ color-scheme: light dark; }}</style>
</head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto;">
{body}
</body>
</html>"""

ABBR_SCRIPT = """
<script>
  var abbrTouched = document.getElementById('abbr').value.length > 0;
  document.getElementById('abbr').addEventListener('input', function() { abbrTouched = true; });
  document.getElementById('county').addEventListener('input', function() {
    if (!abbrTouched) {
      document.getElementById('abbr').value = this.value.trim().slice(0, 3).toLowerCase();
    }
  });
</script>
"""


def render_start():
    return """
    <h1>New County Setup</h1>
    <p>Answer a few questions, get a configured copy of every script dropped in the right folder.</p>
    <form method="post"><button type="submit" name="nav" value="next">Start</button></form>
    """


def render_state():
    options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
        for name, _ in STATES
    )
    return f"""
    <h1>State?</h1>
    <form method="post">
      <select name="state" required>
        <option value="" disabled selected>-- choose a state --</option>
        {options}
      </select>
      <br><br>
      <button type="submit" name="nav" value="back">Back</button>
      <button type="submit" name="nav" value="next">Next</button>
    </form>
    """


def render_county():
    county_val = html.escape(wizard.answers.get("COUNTY", ""))
    abbr_val = html.escape(wizard.answers.get("COUNTY_ABBR", ""))
    return f"""
    <h1>County?</h1>
    <form method="post">
      <label>County name (e.g. Summit)<br>
        <input type="text" id="county" name="county" value="{county_val}">
      </label>
      <br><br>
      <label>3-letter folder abbreviation<br>
        <input type="text" id="abbr" name="abbr" value="{abbr_val}" maxlength="3">
      </label>
      <br><br>
      <button type="submit" name="nav" value="back">Back</button>
      <button type="submit" name="nav" value="next">Next</button>
    </form>
    """ + ABBR_SCRIPT


def render_group(i):
    title, fields = FIELD_GROUPS[i]
    rows = ""
    for token, label, default in fields:
        existing = wizard.answers.get(token)
        if isinstance(default, bool):
            checked = existing if existing is not None else default
            rows += f"""
            <label><input type="checkbox" name="{token}" value="1" {"checked" if checked else ""}>
              {html.escape(label)}</label><br><br>
            """
        else:
            value = existing if existing is not None else (default or "")
            rows += f"""
            <label>{html.escape(label)}<br>
              <input type="text" name="{token}" value="{html.escape(value)}" style="width:100%">
            </label><br><br>
            """
    is_last = i == len(FIELD_GROUPS) - 1
    next_label = "Create County" if is_last else "Next"
    return f"""
    <h1>{html.escape(title)}</h1>
    <form method="post">
      {rows}
      <button type="submit" name="nav" value="back">Back</button>
      <button type="submit" name="nav" value="next">{next_label}</button>
    </form>
    """


def render_done():
    path = wizard.answers.get("_created_path", "")
    return f"""
    <h1>Done</h1>
    <p>Created: <code>{html.escape(path)}</code></p>
    <p>Two files in that folder still need to be hand-built for this county's
    real assessor site: <code>$.py</code> and <code>pricer.py</code>. Their
    build specs are sitting right there as <code>$.md</code> and
    <code>pricer.md</code> - upload those two files to an AI assistant (along
    with a look at the county's actual assessor site in dev tools) and have
    it write both scripts from them.</p>
    <p>Once both are in place, run master.py from that folder to start the pipeline.</p>
    <form method="post"><button type="submit" name="nav" value="restart">Set up another county</button></form>
    """


def render_error(message):
    return f"""
    <h1>Hold on</h1>
    <p>{html.escape(message)}</p>
    <form method="post"><button type="submit" name="nav" value="back">Back</button></form>
    """


def render_current_step():
    step = wizard.step
    if step == "start":
        return render_start()
    if step == "state":
        return render_state()
    if step == "county":
        return render_county()
    if step == "done":
        return render_done()
    if step.startswith("group"):
        return render_group(int(step[len("group"):]))
    return "<p>Unknown step.</p>"


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, body):
        page = PAGE_WRAPPER.format(body=body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self):
        self._send_html(render_current_step())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        nav = form.get("nav", [""])[0]
        step = wizard.step

        if step == "start":
            wizard.step_index = STEPS.index("state")

        elif step == "state":
            if nav == "next":
                name = form.get("state", [""])[0]
                if name not in STATE_ABBR_BY_NAME:
                    self._send_html(render_error("Pick a state from the list."))
                    return
                wizard.answers["STATE_ABBR"] = STATE_ABBR_BY_NAME[name]
                wizard.answers["STATE_NAME"] = name
                wizard.step_index += 1
            else:
                wizard.step_index = STEPS.index("start")

        elif step == "county":
            if nav == "next":
                county = form.get("county", [""])[0].strip()
                abbr = form.get("abbr", [""])[0].strip().lower()
                if not county or len(abbr) != 3 or not abbr.isalpha():
                    self._send_html(render_error(
                        "Enter a county name and a 3-letter alphabetic abbreviation."))
                    return
                wizard.answers["COUNTY"] = county
                wizard.answers["COUNTY_ABBR"] = abbr
                wizard.answers["COUNTY_QUERY_SUFFIX"] = f"{county} County, {wizard.answers['STATE_ABBR']}"
                wizard.answers["COUNTY_LABEL"] = f"{county} County"
                state_slug = wizard.answers["STATE_NAME"].lower().replace(" ", "-")
                county_slug = county.lower().replace(" ", "-")
                wizard.answers["OBITUARY_URL"] = (
                    f"https://www.legacy.com/us/obituaries/local/{state_slug}/{county_slug}-county"
                )
                wizard.step_index += 1
            else:
                wizard.step_index -= 1

        elif step.startswith("group"):
            i = int(step[len("group"):])
            title, fields = FIELD_GROUPS[i]
            for token, label, default in fields:
                if isinstance(default, bool):
                    wizard.answers[token] = token in form
                else:
                    wizard.answers[token] = form.get(token, [""])[0].strip()
            if nav == "next":
                if i == len(FIELD_GROUPS) - 1:
                    try:
                        path = build_county(wizard.answers)
                    except FileExistsError as e:
                        self._send_html(render_error(
                            f"{e.args[0]} already exists - delete it or pick a different abbreviation."))
                        return
                    wizard.answers["_created_path"] = path
                    wizard.step_index = STEPS.index("done")
                else:
                    wizard.step_index += 1
            else:
                wizard.step_index = STEPS.index("county") if i == 0 else wizard.step_index - 1

        elif step == "done":
            if nav == "restart":
                wizard.step_index = 0
                wizard.answers = {}

        self._redirect_home()

    def log_message(self, format, *args):
        pass  # quiet - single-user local tool


def main():
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"New County Setup running at {url}")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
