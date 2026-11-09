"""Authenticated, create-only publication; retain partial state for manual recovery."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

sys.dont_write_bytecode = True
from source_version import read


REPOSITORY = "miyako/4d-plugin-hidapi"
BUNDLE = "test/Plugins/HIDAPI.bundle/Contents/"
GENERATED_FILES = {
    BUNDLE + "MacOS/HIDAPI", BUNDLE + "Info.plist",
    BUNDLE + "Resources/English.lproj/InfoPlist.strings",
    BUNDLE + "Windows64/HIDAPI.4DX", BUNDLE + "CodeResources",
    "winbuild/HIDAPI.4DX",
}
GENERATED_TREES = (
    BUNDLE + "_CodeSignature/", "build/HIDAPI.build/", "build/XCBuildData/",
    "test/Plugins/HIDAPI.build/", "Objs/Release/",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")


def run(*args, input=None, env=None):
    return subprocess.run(
        args, input=input, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    ).stdout


def json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def token():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ValueError("Explicit nonempty GH_TOKEN or GITHUB_TOKEN is required")


def api(endpoint, payload=None, asset=None):
    command = [
        "gh", "api", "--method", "GET" if payload is None and asset is None else "POST",
        endpoint, "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]
    if asset is not None:
        command += ["-H", "Content-Type: application/octet-stream", "--input", str(asset)]
    elif payload is not None:
        command += ["--input", "-"]
    response = run(*command, input=json.dumps(payload) if payload is not None else None,
                   env={**os.environ, "GH_TOKEN": token()})
    return json.loads(response, object_pairs_hook=json_object, parse_constant=invalid_constant)


def generated(path):
    return path in GENERATED_FILES or path.startswith(GENERATED_TREES)


def context(sha, *, final=False):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Expected immutable selected SHA")
    if os.environ.get("GITHUB_SHA") != sha:
        raise ValueError("Selected SHA must match immutable GITHUB_SHA")
    if run("git", "rev-parse", "HEAD").strip() != sha:
        raise ValueError("Checkout HEAD differs from selected SHA")
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError(f"Expected repository {REPOSITORY}")
    token()
    entries = run("git", "ls-files", "-v", "-z").split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
        raise ValueError("Hidden index flags (assume-unchanged/skip-worktree) are forbidden")
    changed = set()
    for args in (
        ("diff", "--cached", "--name-only", "--no-renames", "-z", sha, "--"),
        ("diff", "--name-only", "--no-renames", "-z", "--"),
    ):
        changed.update(filter(None, run("git", *args).split("\0")))
    untracked = set(filter(None, run("git", "ls-files", "--others", "-z", "--").split("\0")))
    if not final and (changed or untracked):
        raise ValueError("Initial preflight requires a globally clean checkout")
    if final and any(not generated(path) for path in changed):
        raise ValueError("Tracked source/config differs from selected commit")
    version = read()
    assets = {f"HIDAPI-{version}.zip", f"HIDAPI-{version}.dmg"}
    if final and any(not generated(path) and path not in assets for path in untracked):
        raise ValueError("Untracked source/config or unexpected output in checkout")
    return REPOSITORY, version, f"v{version}"


def positive_id(value):
    return type(value) is int and value > 0


def validate_ref(value):
    if not isinstance(value, dict) or not isinstance(value.get("ref"), str):
        raise ValueError("Malformed tag ref")
    obj = value.get("object")
    if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str) \
            or not SHA.fullmatch(obj["sha"]) or obj.get("type") not in ("commit", "tag"):
        raise ValueError("Malformed tag ref object")
    return value["ref"], obj["sha"], obj["type"]


def available(repo, tag):
    occupied = False
    ids = set()
    page = 1
    while True:
        releases = api(f"repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(releases, list) or len(releases) > 100:
            raise ValueError("Malformed releases page")
        for release in releases:
            if not isinstance(release, dict) or not positive_id(release.get("id")) \
                    or not isinstance(release.get("tag_name"), str) or not release["tag_name"] \
                    or type(release.get("draft")) is not bool \
                    or type(release.get("prerelease")) is not bool \
                    or release["id"] in ids:
                raise ValueError("Malformed or duplicate release entry")
            ids.add(release["id"])
            occupied |= release["tag_name"] == tag
        if not releases:
            break
        page += 1
    refs = api(f"repos/{repo}/git/matching-refs/tags/{quote(tag, safe='')}")
    if not isinstance(refs, list):
        raise ValueError("Malformed matching refs")
    seen = set()
    for entry in refs:
        ref, _, _ = validate_ref(entry)
        if not ref.startswith(f"refs/tags/{tag}") or ref in seen:
            raise ValueError("Malformed or duplicate matching ref")
        seen.add(ref)
        occupied |= ref == f"refs/tags/{tag}"
    if occupied:
        raise ValueError(f"{tag} already has a tag or draft/published release")
    return ids


def preflight(sha):
    repo, version, tag = context(sha)
    available(repo, tag)
    return version, tag


def validate_assets(version, assets):
    expected = [f"HIDAPI-{version}.zip", f"HIDAPI-{version}.dmg"]
    if [str(path) for path in assets] != expected:
        raise ValueError(f"Expected exact ordered release assets: {expected}")
    for path in assets:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"Missing, symlink or empty asset: {path}")
        with path.open("rb"):
            pass


def publish(sha, assets):
    repo, version, tag = context(sha, final=True)
    validate_assets(version, assets)
    ids = available(repo, tag)
    tag_attempted = False
    tag_created = False
    release_attempted = False
    release_id = None
    uploaded = []
    uploading = None
    try:
        tag_attempted = True
        ref = api(f"repos/{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        identity = (f"refs/tags/{tag}", sha, "commit")
        if validate_ref(ref) != identity:
            raise ValueError("Created tag does not match selected tag/SHA")
        tag_created = True
        if validate_ref(api(f"repos/{repo}/git/ref/tags/{tag}")) != identity:
            raise ValueError("Remote tag does not match selected tag/SHA")
        release_attempted = True
        release = api(f"repos/{repo}/releases", {
            "tag_name": tag, "name": tag, "target_commitish": sha,
            "draft": False, "prerelease": False,
            "body": "", "generate_release_notes": False,
        })
        if isinstance(release, dict) and positive_id(release.get("id")):
            release_id = release["id"]
        upload_url = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets"
        if release_id is None or release_id in ids or release.get("tag_name") != tag \
                or release.get("name") != tag or release.get("target_commitish") != sha \
                or release.get("draft") is not False or release.get("prerelease") is not False \
                or release.get("body") not in ("", None) \
                or release.get("upload_url") != upload_url + "{?name,label}":
            raise ValueError("Malformed new release response")
        for asset in assets:
            uploading = asset.name
            result = api(f"{upload_url}?name={quote(asset.name, safe='')}", asset=asset)
            if not isinstance(result, dict) or not positive_id(result.get("id")) \
                    or result.get("name") != asset.name or result.get("state") != "uploaded" \
                    or type(result.get("size")) is not int \
                    or result["size"] != asset.stat().st_size:
                raise ValueError(f"Malformed upload response for {asset.name}")
            uploaded.append(asset.name)
            uploading = None
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        state = [
            f"tag {tag} retained" if tag_created else
            f"tag {tag} creation may have succeeded" if tag_attempted else "tag not attempted",
            f"release ID {release_id} retained" if release_id is not None else
            "release creation may have succeeded; ID unknown" if release_attempted else
            "release not attempted",
            "uploaded assets retained: " + (", ".join(uploaded) or "(none confirmed)"),
        ]
        if uploading is not None:
            state.append(f"upload of {uploading} may have succeeded")
        raise ValueError(
            f"{error}. Manual recovery required: {'; '.join(state)}. "
            "No retry, rollback, update or deletion was performed."
        ) from error
    print(f"Created {tag} at {sha}; release ID {release_id}; assets: {', '.join(uploaded)}")
    return release_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "check", "publish"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("assets", nargs="*", type=Path)
    args = parser.parse_intermixed_args(argv)
    if args.command in ("preflight", "check"):
        if args.assets:
            parser.error("preflight/check accepts no assets")
        version, tag = preflight(args.sha)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"version={version}\ntag={tag}\n")
    else:
        publish(args.sha, args.assets)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}. Stopped without rollback/retry; inspect retained state.",
              file=sys.stderr)
        sys.exit(1)
