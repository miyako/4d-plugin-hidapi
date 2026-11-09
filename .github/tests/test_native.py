"""Execute actual workflow scripts with a closed PATH and native-only stubs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def block(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name)
    run = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("        run:"))
    if lines[run] != "        run: |":
        return lines[run].split("run: ", 1)[1] + "\n"
    result = []
    for line in lines[run + 1:]:
        if line and not line.startswith("          "):
            break
        result.append(line[10:])
    return ("\n".join(result) + "\n").replace(
        "${{ env.PRODUCT_NAME }}", "HIDAPI"
    ).replace("${{ env.BASE_DIR }}", "test").replace(
        "${{ env.MACOS_DEPLOYMENT_TARGET }}", "10.13"
    )


STUB = r'''
import json, os, pathlib, plistlib, sys
p = pathlib.Path
tool = p(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["NATIVE_LOG"], "a") as out:
    out.write(json.dumps([tool, args]) + "\n")
if os.environ.get("FAIL_TOOL") == tool:
    sys.exit(7)
bundle = p("test/Plugins/HIDAPI.bundle")
if tool == "base64":
    p(args[args.index("-o") + 1]).write_bytes(b"fixture certificate")
elif tool == "security":
    if args[0] == "find-identity":
        print('  1) ABCDEF "Developer ID Application: Fixture"')
elif tool == "xcodebuild":
    assert args[args.index("-project")+1] == "HIDAPI.xcodeproj"
    assert args[args.index("-target")+1] == "HIDAPI"
    assert args[args.index("-configuration")+1] == "Default"
    assert "CONFIGURATION_BUILD_DIR=" + str(p.cwd() / "test/Plugins") in args
    for arg in ("ARCHS=arm64 x86_64", "ONLY_ACTIVE_ARCH=NO",
                "MACOSX_DEPLOYMENT_TARGET=10.13", "CODE_SIGNING_ALLOWED=NO"):
        assert arg in args
    for path in ["Contents/MacOS/HIDAPI", "Contents/Info.plist",
                 "Contents/Resources/English.lproj/InfoPlist.strings",
                 "Contents/CodeResources", "Contents/_CodeSignature"]:
        assert not (bundle/path).exists(), "stale output survived: " + path
    for path in ("build/HIDAPI.build", "build/XCBuildData", "test/Plugins/HIDAPI.build"):
        assert not p(path).exists()
    if os.environ.get("SKIP_BUILD"):
        sys.exit(0)
    sys.path.insert(0, str(p(".github/scripts").resolve()))
    import source_version
    data = plistlib.loads(p("Info.plist").read_bytes())
    version = source_version.read()
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        if key in data:
            data[key] = version
    if os.environ.get("BAD_PLIST"):
        data["CFBundleShortVersionString"] = "0.0.0"
    (bundle/"Contents/MacOS").mkdir(parents=True, exist_ok=True)
    (bundle/"Contents/MacOS/HIDAPI").write_bytes(b"fresh native fixture")
    (bundle/"Contents/Info.plist").write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    text = p("English.lproj/InfoPlist.strings").read_bytes().decode("utf-8")
    if os.environ.get("BAD_LOCALIZATION"):
        text = text.replace(version, "0.0.0")
    dest = bundle/"Contents/Resources/English.lproj/InfoPlist.strings"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(text.encode("utf-16"))
elif tool == "lipo":
    archs = os.environ.get("ARCHS_FIXTURE", "arm64 x86_64").split()
    if args[0] == "-info":
        print("Architectures in the fat file: " + " ".join(archs))
    elif args[0] == "-verify_arch":
        sys.exit(0 if all(a in archs for a in args[1:-1]) else 1)
elif tool == "codesign":
    if "--verify" in args:
        assert "--deep" in args and "--strict" in args
        if os.environ.get("FAIL_VERIFY"):
            sys.exit(8)
    else:
        assert "--options=runtime" in args
        dest = bundle/"Contents/_CodeSignature/CodeResources"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"new signature")
elif tool in ("ditto", "hdiutil"):
    p(args[-1]).write_bytes(b"fresh archive fixture")
elif tool == "xcrun":
    if args[:2] == ["notarytool", "submit"]:
        kind = "ZIP" if args[2].endswith(".zip") else "DMG"
        status = os.environ.get(kind + "_STATUS", "Accepted")
        if status == "malformed":
            print("not json")
        elif status == "list":
            print("[]")
        elif status == "missing":
            print("{}")
        else:
            print(json.dumps({"status": status}))
        sys.exit(int(os.environ.get(kind + "_EXIT", "0")))
    elif args[:2] == ["stapler", "staple"] and os.environ.get("FAIL_STAPLE"):
        sys.exit(9)
'''


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / ".github/scripts", self.root / ".github/scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for path in ("Info.plist", "English.lproj/InfoPlist.strings",
                     "HIDAPI.xcodeproj/project.pbxproj", "manifest.json", "constants.xlf"):
            dest = self.root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, dest)
        self.bundle = self.root / "test/Plugins/HIDAPI.bundle"
        self.preserved = {}
        for name in ("Contents/Resources/manifest.json", "Contents/Resources/constants.xlf"):
            self.seed(self.bundle / name, (ROOT / Path(name).name).read_bytes(), preserve=True)
        for name in ("Contents/Windows64/HIDAPI.4DX", "Contents/QuickLook/Thumbnail.png",
                     "Contents/Resources/untouched.bin", "Contents/Frameworks/a.framework/Versions/A/a"):
            self.seed(self.bundle / name, b"preserved fixture", preserve=True)
        for name in ("test/Plugins/Default/sibling.bundle/Contents/Resources/keep",
                     "lib/windows64/hidapi.lib", "lib/windows64/jsoncpp.lib", "a/libhidapi.a",
                     "a/libjsoncpp.a", "build/other/keep", "Objs/Debug/keep"):
            self.seed(self.root / name, b"preserved sibling", preserve=True)
        for name in ("Contents/MacOS/HIDAPI", "Contents/Info.plist",
                     "Contents/Resources/English.lproj/InfoPlist.strings",
                     "Contents/CodeResources", "Contents/_CodeSignature/CodeResources"):
            self.seed(self.bundle / name, b"stale")
        for name in ("build/HIDAPI.build/stale.o", "build/XCBuildData/build.db",
                     "test/Plugins/HIDAPI.build/stale.o"):
            self.seed(self.root / name, b"stale")
        tools = self.root / "tools"
        tools.mkdir()
        stub = tools / "stub"
        stub.write_text("#!" + sys.executable + "\n" + STUB)
        stub.chmod(0o755)
        for tool in ("xcodebuild", "lipo", "codesign", "security", "ditto", "hdiutil", "xcrun", "base64"):
            (tools / tool).symlink_to(stub)
        (tools / "python3").symlink_to(sys.executable)
        for tool in ("grep", "head", "sed", "cmp", "ls", "mkdir", "cp"):
            (tools / tool).symlink_to(shutil.which(tool))
        runner_temp = self.root / "runner"
        runner_temp.mkdir()
        sys.path.insert(0, str(ROOT / ".github/scripts"))
        import source_version
        self.env = {
            "PATH": str(tools), "HOME": str(self.root),
            "PRODUCT_NAME": "HIDAPI", "BASE_DIR": "test",
            "MACOS_DEPLOYMENT_TARGET": "10.13", "VERSION": source_version.read(),
            "RUNNER_TEMP": str(runner_temp), "GITHUB_ENV": str(self.root / "env"),
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "NATIVE_LOG": str(self.root / "log"), "CERT_BASE64": "fixture",
            "CERT_PASSWORD": "fixture", "KEYCHAIN_PASSWORD": "fixture",
            "APPLE_ID": "fixture", "TEAM_ID": "fixture", "APP_PASSWORD": "fixture",
        }

    def seed(self, path, data, preserve=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if preserve:
            self.preserved[path] = data

    def run_step(self, name, success=True, powershell=False, **env):
        self.env.update(env)
        command = ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", block(name)]
        if powershell:
            command = [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive",
                       "-Command", block(name)]
        result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        if (self.root / "env").exists():
            for line in (self.root / "env").read_text().splitlines():
                key, value = line.split("=", 1)
                self.env[key] = value
        return result

    def build(self, **env):
        self.run_step("build macOS plugin (universal)", **env)
        self.run_step("verify universal binary and fresh metadata")

    def calls(self):
        return [json.loads(line) for line in (self.root / "log").read_text().splitlines()]

    def test_selected_source_version_outputs(self):
        self.run_step("derive source version")
        self.assertEqual((self.root / "outputs").read_text(),
                         f"tag=v{self.env['VERSION']}\nversion={self.env['VERSION']}\n")

    def test_complete_native_gates_and_preserved_resources(self):
        self.run_step("import Developer ID certificate")
        self.run_step("store notarytool credentials")
        self.build()
        self.seed(self.root / "winbuild/HIDAPI.4DX", b"downloaded fresh windows")
        self.run_step("merge Windows binaries into the bundle")
        self.preserved.pop(self.bundle / "Contents/Windows64/HIDAPI.4DX")
        self.assertEqual((self.bundle / "Contents/Windows64/HIDAPI.4DX").read_bytes(),
                         b"downloaded fresh windows")
        self.run_step("codesign")
        self.run_step("package, notarize & staple")
        for path, data in self.preserved.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        submissions = [args for tool, args in self.calls()
                       if tool == "xcrun" and args[:2] == ["notarytool", "submit"]]
        self.assertEqual([args[2] for args in submissions],
                         [f"HIDAPI-{self.env['VERSION']}.zip", f"HIDAPI-{self.env['VERSION']}.dmg"])
        self.assertTrue(all("--wait" in args and "--output-format" in args for args in submissions))
        self.assertEqual(self.calls()[-1], ["xcrun", ["stapler", "staple",
                                                    f"HIDAPI-{self.env['VERSION']}.dmg"]])

    def test_windows_cleanup_and_exact_output_powershell(self):
        binary = self.bundle / "Contents/Windows64/HIDAPI.4DX"
        self.seed(self.root / "Objs/Release/stale.obj", b"stale")
        self.run_step("clean stale outputs", powershell=True)
        self.assertFalse(binary.exists())
        self.assertFalse((self.root / "Objs/Release").exists())
        self.run_step("verify exact Windows output", success=False, powershell=True)
        self.seed(binary, b"fresh windows")
        self.run_step("verify exact Windows output", powershell=True)
        self.seed(binary, b"")
        self.run_step("verify exact Windows output", success=False, powershell=True)
        self.seed(self.root / "elsewhere/Plugins/HIDAPI.4DX", b"wrong location")
        binary.unlink()
        self.run_step("verify exact Windows output", success=False, powershell=True)
        for path, data in self.preserved.items():
            if path != binary:
                self.assertEqual(path.read_bytes(), data)

    def test_windows_cleanup_error_propagates(self):
        binary = self.bundle / "Contents/Windows64/HIDAPI.4DX"
        binary.unlink()
        binary.mkdir()
        self.run_step("clean stale outputs", success=False, powershell=True)

    def test_mac_cleanup_rejects_symlink_before_deleting(self):
        signature = self.bundle / "Contents/_CodeSignature"
        shutil.rmtree(signature)
        signature.symlink_to(self.root / "build/other", target_is_directory=True)
        self.run_step("build macOS plugin (universal)", success=False)
        self.assertEqual((self.bundle / "Contents/MacOS/HIDAPI").read_bytes(), b"stale")

    def test_architecture_tokens_and_extras(self):
        self.build(ARCHS_FIXTURE="arm64 x86_64 i386")
        for archs in ("arm64", "x86_64", "arm64e x86_64", "arm64 x86_64h", "fakearm64 x86_64"):
            with self.subTest(archs=archs):
                self.run_step("verify universal binary and fresh metadata", success=False, ARCHS_FIXTURE=archs)

    def test_no_build_cannot_reuse_stale_native(self):
        self.run_step("build macOS plugin (universal)", SKIP_BUILD="1")
        self.run_step("verify universal binary and fresh metadata", success=False)

    def test_metadata_drift_rejected(self):
        for env in ({"BAD_PLIST": "1"}, {"BAD_LOCALIZATION": "1"}):
            with self.subTest(env=env):
                self.env.pop("BAD_PLIST", None)
                self.env.pop("BAD_LOCALIZATION", None)
                self.run_step("build macOS plugin (universal)", **env)
                self.run_step("verify universal binary and fresh metadata", success=False)

    def test_required_copied_resources_are_verified(self):
        self.build()
        for name in ("manifest.json", "constants.xlf"):
            with self.subTest(name=name):
                path = self.bundle / "Contents/Resources" / name
                original = path.read_bytes()
                path.write_bytes(b"stale resource")
                self.run_step("verify universal binary and fresh metadata", success=False)
                path.write_bytes(original)

    def test_build_failure(self):
        self.run_step("build macOS plugin (universal)", success=False, FAIL_TOOL="xcodebuild")

    def test_native_architecture_tool_failure(self):
        self.build()
        self.run_step("verify universal binary and fresh metadata", success=False, FAIL_TOOL="lipo")

    def test_signing_and_strict_verification_failures(self):
        self.build()
        self.env["KEYCHAIN_PATH"] = "fixture"
        self.run_step("codesign", success=False, FAIL_TOOL="codesign")
        self.env.pop("FAIL_TOOL")
        self.run_step("codesign", success=False, FAIL_VERIFY="1")

    def test_package_tool_failures(self):
        self.build()
        self.env["KEYCHAIN_PATH"] = "fixture"
        for tool in ("ditto", "hdiutil"):
            with self.subTest(tool=tool):
                self.run_step("package, notarize & staple", success=False, FAIL_TOOL=tool)

    def test_notary_status_and_exit_gates_for_both_archives(self):
        self.build()
        self.env["KEYCHAIN_PATH"] = "fixture"
        for kind in ("ZIP", "DMG"):
            for status, exit_code in (("Invalid", "0"), ("malformed", "0"), ("missing", "0"),
                                      ("list", "0"), ("Accepted", "1")):
                with self.subTest(kind=kind, status=status, exit_code=exit_code):
                    for key in ("ZIP_STATUS", "DMG_STATUS", "ZIP_EXIT", "DMG_EXIT"):
                        self.env.pop(key, None)
                    self.run_step("package, notarize & staple", success=False,
                                  **{kind + "_STATUS": status, kind + "_EXIT": exit_code})

    def test_staple_failure(self):
        self.build()
        self.env["KEYCHAIN_PATH"] = "fixture"
        self.run_step("package, notarize & staple", success=False, FAIL_STAPLE="1")

    def test_merge_requires_exact_nonempty_download(self):
        self.build()
        self.seed(self.root / "winbuild/nested/HIDAPI.4DX", b"not exact")
        self.run_step("merge Windows binaries into the bundle", success=False)
        self.seed(self.root / "winbuild/HIDAPI.4DX", b"")
        self.run_step("merge Windows binaries into the bundle", success=False)


if __name__ == "__main__":
    unittest.main()
