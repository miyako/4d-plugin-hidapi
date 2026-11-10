#!/usr/bin/env python3
"""Run the final harness against immutable baseline sources, then current sources."""

import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "b79e1b7fa9cace5c30aeb62113ac1e2d01419a20"
COMMAND = [sys.executable, "-m", "unittest", "discover", "-s", ".github/tests",
           "-p", "test_string_ownership.py", "-v"]


def main():
    with tempfile.TemporaryDirectory(prefix="hidapi-baseline-") as directory:
        archive = subprocess.check_output(["git", "archive", BASELINE,
                                           "4D Plugin API", "support"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            files.extractall(directory, filter="data")
        baseline = subprocess.run(COMMAND, cwd=ROOT, text=True, capture_output=True,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                       "HIDAPI_OWNERSHIP_SOURCE": directory})
        report = baseline.stdout + baseline.stderr
        print(f"Immutable baseline {BASELINE}:\n{report}", flush=True)
        if (baseline.returncode != 1
                or any(f"setter overload {i} leaked 3 SDK strings (allocatedEmpty=1)" not in report
                       for i in range(4))
                or report.count("getters leaked 5 owning property strings") != 2
                or "contracts null: zero SDK strings, handles, and references" not in report
                or "contracts allocated: zero SDK strings, handles, and references" not in report
                or "FAILED (failures=6)" not in report):
            raise RuntimeError("Baseline did not fail for the expected ownership defects")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("HIDAPI_OWNERSHIP_SOURCE", None)
    print("Current source:", flush=True)
    subprocess.run(COMMAND, cwd=ROOT, check=True, env=environment)


if __name__ == "__main__":
    main()
