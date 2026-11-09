#!/usr/bin/env python3
"""Prove the actual guarded helper from the exact final commit against bare Git."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"


def run(*args, cwd=ROOT, capture=True, env=None):
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, env=env)
    return result.stdout.strip() if capture else None


def prove(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("An exact final commit SHA is required")
    if run("git", "rev-parse", "HEAD") != commit or run("git", "status", "--porcelain"):
        raise ValueError("Final checkout must be clean at the requested commit")
    branch = run("git", "symbolic-ref", "--short", "HEAD")
    with tempfile.TemporaryDirectory(prefix=".hidapi-final-bump-", dir=ROOT / ".github/tests") as directory:
        temp = Path(directory)
        bare, checkout = temp / "remote.git", temp / "checkout"
        run("git", "clone", "--quiet", "--bare", "--no-hardlinks", "--no-tags",
            "--single-branch", "--branch", branch, str(ROOT), str(bare))
        run("git", "clone", "--quiet", "--no-hardlinks", "--no-tags", "--single-branch",
            str(bare), str(checkout))
        run("git", "checkout", "--quiet", "--detach", commit, cwd=checkout)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL", "EMAIL"):
            env.pop(key, None)
        source_paths = set(json.loads(run(sys.executable, "-c",
            'import json,sys;sys.path.insert(0,".github/scripts");'
            'from source_version import SOURCE_PATHS;print(json.dumps(list(map(str,SOURCE_PATHS))))',
            cwd=checkout, env=env)))
        before = run(sys.executable, ".github/scripts/source_version.py", cwd=checkout, env=env)
        run(sys.executable, ".github/scripts/bump.py", "patch", "--sha", commit,
            "--ref", "refs/heads/" + branch, cwd=checkout, capture=False, env=env)
        after = run(sys.executable, ".github/scripts/source_version.py", cwd=checkout, env=env)
        major, minor, patch = map(int, before.split("."))
        if after != f"{major}.{minor}.{patch+1}":
            raise ValueError("Actual helper did not perform a patch bump")
        child = run("git", "rev-parse", "HEAD", cwd=checkout)
        advertised = run("git", "--git-dir=" + str(bare), "rev-parse", "refs/heads/" + branch)
        parents = run("git", "show", "-s", "--format=%P", child, cwd=checkout).split()
        changed = set(run("git", "diff-tree", "--no-commit-id", "--name-only", "-r", child,
                          cwd=checkout).splitlines())
        expected = source_paths - {"Info.plist"}
        if child != advertised or parents != [commit] or changed != expected:
            raise ValueError("Bump must be an exact-source direct child equal to remote")
        if TRAILER not in run("git", "show", "-s", "--format=%B", child, cwd=checkout).splitlines():
            raise ValueError("Generated Bump coauthor trailer is missing")
        for git_dir in (checkout / ".git", bare):
            if run("git", "--git-dir=" + str(git_dir), "tag", "--list"):
                raise ValueError("Disposable local or remote tags were created")
        if run("git", "status", "--porcelain", cwd=checkout):
            raise ValueError("Generated Bump left a dirty checkout")
        run(sys.executable, "-m", "unittest", "discover", "-s", ".github/tests", "-v",
            cwd=checkout, capture=False, env=env)
        run("actionlint", cwd=checkout, capture=False, env=env)
        if run("git", "status", "--porcelain", cwd=checkout):
            raise ValueError("Bumped full validation dirtied the disposable checkout")
        if run("git", "rev-parse", "HEAD", cwd=checkout) != child \
                or run("git", "--git-dir=" + str(bare), "rev-parse", "refs/heads/" + branch) != child:
            raise ValueError("Bumped full validation changed local or bare HEAD")
        for git_dir in (checkout / ".git", bare):
            if run("git", "--git-dir=" + str(git_dir), "tag", "--list"):
                raise ValueError("Bumped full validation created tags")
        proof = {"migration": commit, "local": child, "bare": advertised, "parents": parents,
                 "before": before, "after": after, "source_paths": sorted(changed), "clean": True,
                 "local_tags": 0, "remote_tags": 0, "coauthor": TRAILER}
    proof["fixture_removed"] = not temp.exists()
    if run("git", "rev-parse", "HEAD") != commit or run("git", "status", "--porcelain"):
        raise ValueError("Proof changed the final migration checkout")
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    prove(parser.parse_args().commit)
