"""Exercise the actual guarded push against disposable local bare origins."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import bump as lifecycle
import source_version as source


def git(root, *args, input=None):
    location = ["--git-dir", str(root)] if root.name.endswith(".git") else ["-C", str(root)]
    return subprocess.run(["git", *location, *args], input=input, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class BumpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".lifecycle-", dir=ROOT / ".github/tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local, self.remote = self.root / "local", self.root / "remote.git"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "selected", str(self.local))
        git(self.local, "config", "user.name", "Offline fixture")
        git(self.local, "config", "user.email", "fixture@example.invalid")
        git(self.local, "config", "commit.gpgsign", "false")
        git(self.local, "config", "push.followTags", "true")
        git(self.local, "commit", "--allow-empty", "-m", "ancestor")
        self.ancestor = git(self.local, "rev-parse", "HEAD")
        for path in source.SOURCE_PATHS:
            dest = self.local / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, dest)
        shutil.copytree(ROOT / ".github/scripts", self.local / ".github/scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git(self.local, "add", ".")
        git(self.local, "commit", "-m", "selected source")
        self.sha = git(self.local, "rev-parse", "HEAD")
        git(self.local, "remote", "add", "origin", str(self.remote))
        git(self.local, "push", "--no-follow-tags", "origin", "HEAD:refs/heads/selected")
        self.ref = "refs/heads/selected"
        self.guard = self.local / ".github/scripts/bump-hooks/pre-push"
        previous = Path.cwd()
        os.chdir(self.local)
        self.addCleanup(os.chdir, previous)
        self.start(patch.object(lifecycle, "__file__", str(self.local / ".github/scripts/bump.py")))

    def start(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def bump(self):
        lifecycle.bump_branch(self.sha, self.ref, "patch")

    def remote_sha(self):
        return lifecycle.remote_ref(self.ref)

    def test_real_bump_source_only_parent_trailer_no_follow_tags(self):
        git(self.local, "tag", "-a", "must-not-follow", "-m", "unrelated")
        git(self.remote, "update-ref", "refs/heads/other", self.sha)
        self.bump()
        new = git(self.local, "rev-parse", "HEAD")
        self.assertEqual(self.remote_sha(), new)
        self.assertEqual(git(self.local, "rev-list", "--parents", "-n", "1", new), f"{new} {self.sha}")
        self.assertEqual(git(self.local, "status", "--porcelain"), "")
        self.assertEqual(git(self.remote, "tag", "--list"), "")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/other"), self.sha)
        changed = set(git(self.local, "diff-tree", "--no-commit-id", "--name-only", "-r", new).splitlines())
        self.assertTrue(changed)
        self.assertLessEqual(changed, set(map(str, source.SOURCE_PATHS)))
        self.assertIn(lifecycle.TRAILER, git(self.local, "log", "-1", "--format=%B"))
        self.assertEqual(git(self.local, "config", "push.followTags"), "true")

    def test_cli_detached_no_identity_or_bytecode_workaround(self):
        git(self.local, "config", "--unset-all", "user.name")
        git(self.local, "config", "--unset-all", "user.email")
        git(self.local, "checkout", "--detach", self.sha)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_"))
               and key not in ("EMAIL", "PYTHONDONTWRITEBYTECODE")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run([sys.executable, ".github/scripts/bump.py", "patch",
                        "--sha", self.sha, "--ref", self.ref], env=env, check=True,
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(git(self.local, "status", "--porcelain"), "")
        self.assertEqual(self.remote_sha(), git(self.local, "rev-parse", "HEAD"))

    def test_initial_sha_ref_dirty_and_hidden_flags(self):
        for sha, ref in (("main", self.ref), ("a" * 40, self.ref), (self.sha, "refs/tags/tag")):
            with self.assertRaises(ValueError):
                lifecycle.bump_branch(sha, ref, "patch")
        (self.local / "dirty").write_text("dirty")
        with self.assertRaises(ValueError):
            self.bump()
        (self.local / "dirty").unlink()
        for flag, undo in (("--assume-unchanged", "--no-assume-unchanged"),
                           ("--skip-worktree", "--no-skip-worktree")):
            git(self.local, "update-index", flag, str(source.PLIST))
            with self.assertRaisesRegex(ValueError, "index flags"):
                self.bump()
            git(self.local, "update-index", undo, str(source.PLIST))
        self.assertEqual(self.remote_sha(), self.sha)

    def test_initial_and_late_missing_nonexecutable_guard(self):
        original = self.guard.read_bytes()
        real = lifecycle.remote_ref
        for late in (False, True):
            for missing in (False, True):
                self.guard.write_bytes(original)
                self.guard.chmod(0o755)
                # Use an external runtime hook so fixture changes are not dirty source.
                runtime = self.root / f"runtime-{late}-{missing}"
                shutil.copytree(self.local / ".github/scripts", runtime)
                guard = runtime / "bump-hooks/pre-push"
                reads = 0

                def lose():
                    guard.unlink() if missing else guard.chmod(0o644)

                def remote(ref):
                    nonlocal reads
                    result = real(ref)
                    reads += 1
                    if late and reads == 2:
                        lose()
                    return result

                if not late:
                    lose()
                with patch.object(lifecycle, "__file__", str(runtime / "bump.py")), \
                        patch.object(lifecycle, "remote_ref", side_effect=remote), \
                        patch.object(lifecycle, "run", wraps=lifecycle.run) as run:
                    with self.assertRaisesRegex(ValueError, "guard is missing or not executable"):
                        self.bump()
                    self.assertFalse(any("push" in c.args for c in run.call_args_list))
                git(self.local, "reset", "--hard", self.sha)
        self.assertEqual(self.remote_sha(), self.sha)

    def advertised_race(self, kind):
        real = lifecycle.run
        reads = 0
        expected = None

        def race(*args, **kwargs):
            nonlocal reads, expected
            if args[:2] == ("git", "ls-remote"):
                reads += 1
            if "push" in args:
                self.assertEqual(reads, 2)
                if kind == "delete":
                    git(self.remote, "update-ref", "-d", self.ref)
                else:
                    if kind == "rewind":
                        expected = self.ancestor
                    elif kind == "same":
                        expected = git(self.local, "rev-parse", "HEAD")
                        git(self.remote, "fetch", "--no-tags", str(self.local), "HEAD")
                    else:
                        expected = git(self.local, "commit-tree", "HEAD^{tree}",
                                       "-p", self.sha, input="concurrent\n")
                        git(self.remote, "fetch", "--no-tags", str(self.local), expected)
                    git(self.remote, "update-ref", self.ref, expected)
            return real(*args, **kwargs)

        with patch.object(lifecycle, "run", side_effect=race), \
                self.assertRaises(subprocess.CalledProcessError):
            self.bump()
        self.assertEqual(self.remote_sha(), expected)
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_advertised_advance(self):
        self.advertised_race("advance")

    def test_advertised_rewind(self):
        self.advertised_race("rewind")

    def test_advertised_deletion(self):
        self.advertised_race("delete")

    def test_advertised_same_result_zero_updates(self):
        self.advertised_race("same")

    def test_corrupt_commit_parent_source_empty_trailer(self):
        real = lifecycle.run
        for kind in ("merge", "non-source", "empty", "trailer"):
            git(self.local, "reset", "--hard", self.sha)

            def corrupt(*args, **kwargs):
                result = real(*args, **kwargs)
                if "commit" in args:
                    if kind == "non-source":
                        (self.local / "unrelated").write_text("unrelated")
                        git(self.local, "add", "unrelated")
                        git(self.local, "commit", "--amend", "--no-edit")
                    else:
                        tree = self.sha + "^{tree}" if kind == "empty" else "HEAD^{tree}"
                        parents = ["-p", self.sha]
                        if kind == "merge":
                            parents += ["-p", self.ancestor]
                        message = "replacement\n\n" + ("" if kind == "trailer" else lifecycle.TRAILER)
                        new = git(self.local, "commit-tree", tree, *parents, input=message)
                        git(self.local, "reset", "--hard", new)
                return result

            with patch.object(lifecycle, "run", side_effect=corrupt) as run:
                with self.assertRaises(ValueError):
                    self.bump()
                self.assertFalse(any("push" in c.args for c in run.call_args_list))
            self.assertEqual(self.remote_sha(), self.sha)

    def test_hook_exact_ref_sha_head_parent_and_update_count(self):
        git(self.local, "commit", "--allow-empty", "-m", "child")
        child = git(self.local, "rev-parse", "HEAD")
        env = {**os.environ, "BUMP_EXPECTED_SHA": self.sha,
               "BUMP_EXPECTED_REF": self.ref, "BUMP_NEW_SHA": child}
        valid = f"{child} {child} {self.ref} {self.sha}\n"

        def invoke(row, environment=env):
            return subprocess.run([str(self.guard)], input=row, env=environment,
                                  text=True, capture_output=True).returncode

        self.assertEqual(invoke(valid), 0)
        for row in ("", valid * 2, valid.replace(self.ref, "refs/heads/other"),
                    valid.replace(self.sha, self.ancestor), valid.replace(child, "HEAD", 1)):
            self.assertNotEqual(invoke(row), 0)
        self.assertNotEqual(invoke(valid, {}), 0)
        git(self.local, "reset", "--hard", self.sha)
        self.assertNotEqual(invoke(valid), 0)


if __name__ == "__main__":
    unittest.main()
