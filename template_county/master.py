import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PIPELINE = [
    "obi.py",
    "$.py",
    "DC.py",
    "pricer.py",
]


def run_step(script_name, mode):
    print(f"\n=== Running {script_name} ({mode}) ===")
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script_name), mode],
        cwd=SCRIPT_DIR,
    )
    if result.returncode != 0:
        print(f"\n{script_name} exited with code {result.returncode}; stopping pipeline.")
        sys.exit(result.returncode)


def prompt_mode():
    while True:
        choice = input("Weekly or daily mode? [weekly/daily]: ").strip().lower()
        if choice in ("weekly", "w"):
            return "weekly"
        if choice in ("daily", "d"):
            return "daily"
        print("Please enter 'weekly' or 'daily'.")


if __name__ == "__main__":
    mode = prompt_mode()
    for script in PIPELINE:
        run_step(script, mode)
    print("\nPipeline complete.")
