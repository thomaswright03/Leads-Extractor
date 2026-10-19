import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PIPELINE = [
    "SLKobi.py",
    "SLK$.py",
    "DC.py",
    "pricer.py",
]


# Written after every run (including a failed one) and committed by the
# nightly workflow - dashboard.py's /admin page reads it to show which step
# failed and the error lines it printed, without anyone digging through
# GitHub Actions logs.
PIPELINE_RUN_PATH = os.path.join(SCRIPT_DIR, ".last_pipeline_run.json")
ERROR_LINE_PATTERN = re.compile(r"error|traceback|exception|giving up|warning|failed|captcha|blocked", re.IGNORECASE)
MAX_ERROR_LINES = 40


def write_pipeline_run(run):
    run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(PIPELINE_RUN_PATH, "w") as f:
        json.dump(run, f, indent=1)


def run_step(script_name, mode, run):
    print(f"\n=== Running {script_name} ({mode}) ===", flush=True)
    started = time.time()
    error_lines = []
    # Echo the step's output live (same as before) while keeping the lines
    # that look like errors for the /admin page.
    process = subprocess.Popen(
        [sys.executable, os.path.join(SCRIPT_DIR, script_name), mode],
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    for line in process.stdout:
        print(line, end="", flush=True)
        if ERROR_LINE_PATTERN.search(line):
            error_lines.append(line.rstrip()[:300])
    returncode = process.wait()
    run["steps"].append({
        "script": script_name,
        "returncode": returncode,
        "seconds": round(time.time() - started),
        "error_count": len(error_lines),
        "error_lines": error_lines[-MAX_ERROR_LINES:],
    })
    if returncode != 0:
        print(f"\n{script_name} exited with code {returncode}; stopping pipeline.")
        run["ok"] = False
        run["failed_step"] = script_name
        write_pipeline_run(run)
        sys.exit(returncode)


def prompt_mode():
    while True:
        choice = input("Weekly or daily mode? [weekly/daily]: ").strip().lower()
        if choice in ("weekly", "w"):
            return "weekly"
        if choice in ("daily", "d"):
            return "daily"
        print("Please enter 'weekly' or 'daily'.")


if __name__ == "__main__":
    # Non-interactive when a mode is passed as an argv (e.g. `master.py daily`
    # from a cron job / CI), otherwise falls back to the interactive prompt
    # for manual runs.
    if len(sys.argv) > 1:
        mode = sys.argv[1].strip().lower()
        if mode not in ("weekly", "daily"):
            print(f"Invalid mode '{mode}' - expected 'weekly' or 'daily'.")
            sys.exit(1)
    else:
        mode = prompt_mode()
    run = {
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "failed_step": None,
        "steps": [],
    }
    for script in PIPELINE:
        run_step(script, mode, run)
    write_pipeline_run(run)
    print("\nPipeline complete.")
