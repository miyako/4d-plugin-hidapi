"""Push one source-only direct-child version commit over the selected branch SHA."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
from source_version import SOURCE_PATHS, bump


TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"


def run(*args, env=None):
    return subprocess.run(
        args, env=env, text=True, stdout=subprocess.PIPE, check=True,
    ).stdout.strip()


def selected_sha(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout does not match immutable selected SHA")


def visible_index():
    entries = run("git", "ls-files", "-v", "-z").split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
        raise ValueError("Hidden index flags (assume-unchanged/skip-worktree) are forbidden")


def clean():
    visible_index()
    if run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Bump requires a clean checkout")


def remote_ref(ref):
    lines = run("git", "ls-remote", "--refs", "origin", ref).splitlines()
    if not lines:
        return None
    parts = lines[0].split()
    if len(lines) != 1 or len(parts) != 2 or parts[1] != ref \
            or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
        raise ValueError("Malformed remote branch advertisement")
    return parts[0]


def require_guard(guard):
    if not guard.is_file() or not os.access(guard, os.X_OK):
        raise ValueError("Required advertised-SHA guard is missing or not executable")


def bump_branch(sha, ref, mode):
    selected_sha(sha)
    if not ref.startswith("refs/heads/"):
        raise ValueError("Bump requires a full refs/heads/* branch ref")
    run("git", "check-ref-format", ref)
    hooks = Path(__file__).resolve().parent / "bump-hooks"
    guard = hooks / "pre-push"
    require_guard(guard)
    clean()
    if remote_ref(ref) != sha:
        raise ValueError("Selected branch moved since dispatch")
    new = bump(mode)
    run("git", "add", "--", *map(str, SOURCE_PATHS))
    run("git", "-c", "user.name=github-actions[bot]",
        "-c", "user.email=github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"chore: bump version to {new}", "-m", TRAILER)
    if remote_ref(ref) != sha:
        raise ValueError("Selected branch moved during preparation")
    commit = run("git", "rev-parse", "HEAD")
    if run("git", "rev-list", "--parents", "-n", "1", commit) != f"{commit} {sha}":
        raise ValueError("Bump must be a single-parent direct child of selected SHA")
    clean()
    changed = set(filter(None, run("git", "diff-tree", "--no-commit-id", "--name-only",
                                   "--no-renames", "-z", "-r", commit).split("\0")))
    if not changed or not changed.issubset(set(map(str, SOURCE_PATHS))):
        raise ValueError("Bump must change source metadata only")
    trailers = run("git", "log", "-1", "--format=%(trailers:only,unfold)", commit)
    if TRAILER not in trailers.splitlines():
        raise ValueError("Bump is missing required co-author trailer")
    require_guard(guard)
    result = run("git", "-c", f"core.hooksPath={hooks}", "-c", "push.followTags=false",
                 "push", "--porcelain", "--no-follow-tags", "origin", f"{commit}:{ref}",
                 env={**os.environ, "BUMP_EXPECTED_SHA": sha,
                      "BUMP_EXPECTED_REF": ref, "BUMP_NEW_SHA": commit})
    if not any(line.startswith(" \t") and line.split("\t")[1] == f"{commit}:{ref}"
               for line in result.splitlines()):
        raise ValueError("Push was not a new exact branch update; inspect retained state")
    if remote_ref(ref) != commit:
        raise ValueError("Pushed branch failed exact-SHA verification")
    print(f"Bumped source metadata to {new}; run Build manually on {ref}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("patch", "minor", "major"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    bump_branch(args.sha, args.ref, args.mode)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}. Stopped without rollback/retry; inspect retained state.",
              file=sys.stderr)
        sys.exit(1)
