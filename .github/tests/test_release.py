"""Actual release CLI with a fake gh executable and real disposable Git state."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import release
import source_version as source


FAKE_GH = r'''
import json, os, pathlib, sys
p = pathlib.Path
args = sys.argv[1:]
assert args[:2] == ["api", "--method"], args
method, endpoint = args[2:4]
assert "Accept: application/vnd.github+json" in args
assert "X-GitHub-Api-Version: 2022-11-28" in args
payload = None
asset = None
if "--input" in args:
    value = args[args.index("--input") + 1]
    if value == "-":
        payload = json.load(sys.stdin)
    else:
        asset = p(value)
record = {"method": method, "endpoint": endpoint, "payload": payload}
with open(".git/api-log", "a") as out:
    out.write(json.dumps(record) + "\n")
base = "repos/miyako/4d-plugin-hidapi"
version = os.environ["FIXTURE_VERSION"]
tag = "v" + version
sha = os.environ["GITHUB_SHA"]
ref = {"ref": "refs/tags/" + tag, "object": {"sha": sha, "type": "commit"}}
uploads = "https://uploads.github.com/" + base + "/releases/123/assets"
if method == "GET" and "/releases?" in endpoint:
    kind = "read"
    result = []
    if os.environ.get("OCCUPIED"):
        result = [{"id": 9, "tag_name": tag, "draft": True, "prerelease": False}]
elif method == "GET" and "/git/matching-refs/" in endpoint:
    kind = "refs"
    result = [ref] if os.environ.get("TAG_OCCUPIED") else []
elif method == "POST" and endpoint.endswith("/git/refs"):
    kind = "tag"
    assert payload == {"ref": "refs/tags/" + tag, "sha": sha}, payload
    result = ref
    p(".git/created-tag").write_text(sha)
elif method == "GET" and "/git/ref/tags/" in endpoint:
    kind = "verify"
    result = ref
elif method == "POST" and endpoint.endswith("/releases"):
    kind = "release"
    assert payload == {"tag_name": tag, "name": tag, "target_commitish": sha,
                       "draft": False, "prerelease": False,
                       "body": "", "generate_release_notes": False}, payload
    result = {**payload, "id": 123, "upload_url": uploads + "{?name,label}"}
    p(".git/created-release").write_text("123")
else:
    assert method == "POST" and endpoint.startswith(uploads + "?name="), endpoint
    kind = "zip" if asset.suffix == ".zip" else "dmg"
    assert asset.name == "HIDAPI-" + version + asset.suffix
    result = {"id": 456, "name": asset.name, "size": asset.stat().st_size, "state": "uploaded"}
    p(".git/uploaded-" + kind).write_text(asset.name)
if os.environ.get("FAIL_AT") == kind:
    sys.exit(7)
if os.environ.get("MALFORMED_AT") == kind:
    print('{"duplicate":1,"duplicate":2}')
elif os.environ.get("OVERRIDE_AT") == kind:
    print(os.environ["OVERRIDE_JSON"])
else:
    print(json.dumps(result))
'''


def entry(identifier=1, tag="v0.0.1", **values):
    return {"id": identifier, "tag_name": tag, "draft": False, "prerelease": False, **values}


class AvailabilityTests(unittest.TestCase):
    def test_pagination_all_pages_drafts_prefix_and_duplicate_ids(self):
        prefix = {"ref": "refs/tags/v1.2.30", "object": {"sha": "a" * 40, "type": "tag"}}
        with patch.object(release, "api", side_effect=[[entry()], [entry(2)], [], [prefix]]) as api:
            self.assertEqual(release.available(release.REPOSITORY, "v1.2.3"), {1, 2})
            self.assertEqual(api.call_count, 4)
        for responses in (
            [[entry()], [entry(2, "v1.2.3", draft=True)], [], []],
            [[entry()], [entry()], []],
            [[entry(id=True)]],
            [{}, []],
            [[], [{"ref": "refs/tags/v1.2.3", "object": {"sha": "a" * 40, "type": "commit"}}]],
        ):
            with patch.object(release, "api", side_effect=responses), self.assertRaises(ValueError):
                release.available(release.REPOSITORY, "v1.2.3")

    def test_errors_on_every_read_stop_without_mutation(self):
        for page in (0, 1, 2):
            responses = [[entry(i + 1)] for i in range(page)] + [OSError("offline error")]
            with patch.object(release, "api", side_effect=responses) as api:
                with self.assertRaises(OSError):
                    release.available(release.REPOSITORY, "v1.2.3")
                self.assertEqual(api.call_count, page + 1)

    def test_strict_json_and_boolean_ids(self):
        for value in (True, False, 0, -1, "1", None):
            self.assertFalse(release.positive_id(value))
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', "not json"):
            with patch.dict(os.environ, GH_TOKEN="offline-fixture"), \
                    patch.object(release, "run", return_value=raw), self.assertRaises(ValueError):
                release.api("repos/fixture/releases")

    def test_malformed_refs_and_duplicate_names_fail_closed(self):
        valid = {"ref": "refs/tags/v1.2.3-extra",
                 "object": {"sha": "a" * 40, "type": "commit"}}
        for refs in ({}, [None], [valid, valid],
                     [{"ref": "refs/heads/other", "object": valid["object"]}],
                     [{"ref": valid["ref"], "object": {"sha": "main", "type": "commit"}}]):
            with patch.object(release, "api", side_effect=[[], refs]), self.assertRaises(ValueError):
                release.available(release.REPOSITORY, "v1.2.3")


class ReleaseCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".release-", dir=ROOT / ".github/tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        (self.tools / "git").symlink_to(shutil.which("git"))
        executable = self.tools / "gh"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        executable.chmod(0o755)
        for path in (*source.SOURCE_PATHS, Path(".github/scripts/source_version.py"),
                     Path(".github/scripts/release.py")):
            dest = self.repo / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, dest)
        self.current = source.read(self.repo)
        self.generated = sorted(release.GENERATED_FILES - {"winbuild/HIDAPI.4DX"})
        self.generated += [release.BUNDLE + "_CodeSignature/CodeResources"]
        self.protected = [
            "plugin.cpp", ".github/workflows/release.yml", ".github/scripts/source_version.py",
            "HIDAPI.xcodeproj/extra.xcconfig", release.BUNDLE + "Resources/manifest.json",
            "build/unproven/input", "a/libhidapi.a",
        ]
        for name in self.generated + [p for p in self.protected if not (self.repo / p).exists()]:
            self.write(name, b"tracked fixture")
        self.git("init", "--quiet")
        self.git("config", "user.name", "Offline fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "selected fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.assets = [f"HIDAPI-{self.current}.zip", f"HIDAPI-{self.current}.dmg"]
        self.env = {
            "PATH": str(self.tools), "HOME": str(self.root),
            "GITHUB_REPOSITORY": release.REPOSITORY, "GH_TOKEN": "offline-fixture",
            "GITHUB_SHA": self.sha, "FIXTURE_VERSION": self.current,
            "GITHUB_OUTPUT": str(self.repo / ".git/output"),
        }

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True, check=True,
                              capture_output=True).stdout.strip()

    def write(self, name, data=b"fixture"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def cli(self, command, assets=None, success=True, **env):
        args = [sys.executable, ".github/scripts/release.py", command, "--sha", self.sha]
        if command == "publish":
            args += self.assets if assets is None else assets
        result = subprocess.run(args, cwd=self.repo, env={**self.env, **env},
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def calls(self):
        path = self.repo / ".git/api-log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def clear_calls(self):
        (self.repo / ".git/api-log").unlink(missing_ok=True)

    def package(self):
        for name in self.assets:
            self.write(name, b"package fixture")

    def test_initial_clean_then_tracked_generated_rewrites_publish_and_retain_bytes(self):
        self.cli("preflight")
        self.assertEqual((self.repo / ".git/output").read_text(),
                         f"version={self.current}\ntag=v{self.current}\n")
        self.clear_calls()
        retained = {name: b"rebuilt bytes " + str(i).encode()
                    for i, name in enumerate(self.generated)}
        for name, raw in retained.items():
            self.write(name, raw)
        self.git("add", "--", self.generated[0])
        self.write("winbuild/HIDAPI.4DX")
        self.write("build/HIDAPI.build/intermediate")
        self.write("build/XCBuildData/build.db")
        self.write("test/Plugins/HIDAPI.build/intermediate")
        self.write("Objs/Release/HIDAPI.obj")
        self.package()
        self.cli("publish")
        self.assertEqual([c["method"] for c in self.calls()],
                         ["GET", "GET", "POST", "GET", "POST", "POST", "POST"])
        for name, raw in retained.items():
            self.assertEqual((self.repo / name).read_bytes(), raw)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.sha)
        self.assertTrue(self.git("status", "--porcelain"))

    def test_initial_rejects_tracked_generated_and_untracked_before_api(self):
        for name in (self.generated[0], "injected.cpp"):
            self.write(name, b"changed")
            self.cli("check", success=False)
            self.assertEqual(self.calls(), [])
            if name in self.generated:
                self.git("restore", "--", name)
            else:
                (self.repo / name).unlink()

    def test_each_source_config_staged_unstaged_and_reversed_fails_before_api(self):
        self.package()
        for name in list(map(str, source.SOURCE_PATHS)) + self.protected:
            original = (self.repo / name).read_bytes()
            for stage in (False, True, "reversed"):
                with self.subTest(path=name, stage=stage):
                    self.write(name, original + b"\nchanged")
                    if stage:
                        self.git("add", "--", name)
                    if stage == "reversed":
                        self.write(name, original)
                    self.cli("publish", success=False)
                    self.assertEqual(self.calls(), [])
                    self.git("restore", "--source=HEAD", "--staged", "--worktree", "--", name)

    def test_untracked_ignored_source_config_and_unproven_outputs_fail(self):
        self.package()
        (self.repo / ".git/info/exclude").write_text("ignored.cpp\n")
        for name in ("injected.cpp", "ignored.cpp", ".github/workflows/extra.yml",
                     "build/unproven/output", release.BUNDLE + "Resources/injected",
                     release.BUNDLE + "Windows64/extra.4DX", "unexpected.zip"):
            path = self.write(name)
            self.cli("publish", success=False)
            self.assertEqual(self.calls(), [])
            self.assertEqual(path.read_bytes(), b"fixture")
            path.unlink()

    def test_hidden_index_flags_fail_before_api(self):
        self.package()
        for flag, undo in (("--assume-unchanged", "--no-assume-unchanged"),
                           ("--skip-worktree", "--no-skip-worktree")):
            for name in (str(source.PLIST), self.generated[0]):
                self.git("update-index", flag, name)
                self.cli("publish", success=False)
                self.assertEqual(self.calls(), [])
                self.git("update-index", undo, name)

    def test_head_repository_and_auth_fail_before_api(self):
        self.package()
        for values in ({"GITHUB_REPOSITORY": "owner/other"}, {"GH_TOKEN": ""},
                       {"GH_TOKEN": " ", "GITHUB_TOKEN": ""}, {"GITHUB_SHA": "a" * 40}):
            self.cli("publish", success=False, **values)
            self.assertEqual(self.calls(), [])
        self.git("commit", "--quiet", "--allow-empty", "-m", "changed HEAD")
        self.cli("publish", success=False)
        self.assertEqual(self.calls(), [])

    def test_wrong_missing_empty_symlink_and_reversed_assets_fail_before_api(self):
        self.package()
        for assets in ([], self.assets[::-1], [self.assets[0]], ["other.zip", self.assets[1]],
                       [str(self.repo / self.assets[0]), self.assets[1]]):
            self.cli("publish", assets=assets, success=False)
            self.assertEqual(self.calls(), [])
        path = self.repo / self.assets[0]
        path.write_bytes(b"")
        self.cli("publish", success=False)
        path.unlink()
        self.cli("publish", success=False)
        path.symlink_to(self.repo / self.assets[1])
        self.cli("publish", success=False)
        self.assertEqual(self.calls(), [])

    def test_each_post_or_verification_failure_retains_created_state_no_retry(self):
        self.package()
        expected = {"tag": 3, "verify": 4, "release": 5, "zip": 6, "dmg": 7}
        for kind, count in expected.items():
            for malformed in (False, True):
                self.clear_calls()
                env = {"MALFORMED_AT" if malformed else "FAIL_AT": kind}
                result = self.cli("publish", success=False, **env)
                self.assertEqual(len(self.calls()), count)
                self.assertIn("Manual recovery required", result.stderr)
                self.assertIn("No retry, rollback, update or deletion", result.stderr)
                self.assertTrue((self.repo / ".git/created-tag").exists())
                self.assertTrue(all(call["method"] in ("GET", "POST") for call in self.calls()))

    def test_malformed_creation_schemas_never_continue(self):
        self.package()
        identity = {"ref": f"refs/tags/v{self.current}",
                    "object": {"sha": self.sha, "type": "commit"}}
        cases = [
            ("tag", {}, 3),
            ("tag", {**identity, "object": {"sha": self.sha, "type": "tag"}}, 3),
            ("verify", {**identity, "object": {"sha": "a" * 40, "type": "commit"}}, 4),
            ("release", {"id": True}, 5),
            ("release", {"id": 123}, 5),
            ("zip", {"id": 456, "name": self.assets[0], "size": True, "state": "uploaded"}, 6),
            ("zip", {"id": True, "name": self.assets[0], "size": 15, "state": "uploaded"}, 6),
            ("dmg", [], 7),
        ]
        for kind, response, count in cases:
            with self.subTest(stage=kind, response=response):
                self.clear_calls()
                self.cli("publish", success=False, OVERRIDE_AT=kind,
                         OVERRIDE_JSON=json.dumps(response))
                self.assertEqual(len(self.calls()), count)

    def test_occupied_draft_or_tag_and_read_failure_never_post(self):
        self.package()
        for values in ({"OCCUPIED": "1"}, {"TAG_OCCUPIED": "1"}, {"FAIL_AT": "read"},
                       {"MALFORMED_AT": "refs"}):
            self.clear_calls()
            self.cli("publish", success=False, **values)
            self.assertTrue(all(call["method"] == "GET" for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
