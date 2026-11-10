"""Anchor the original contract to the immutable intake, not mutable HEAD."""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
INTAKE = "62382f9cc6bc0f22503bdddc641f5016400ba97d"


def original(path):
    return subprocess.check_output(["git", "show", f"{INTAKE}:{path}"], cwd=ROOT)


class ContractTests(unittest.TestCase):
    def test_exactly_two_independent_manual_entries(self):
        workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
        self.assertEqual([p.name for p in workflows], ["bump-version.yml", "release.yml"])
        bump, build = [p.read_text() for p in workflows]
        self.assertTrue(bump.startswith("name: Bump\n"))
        self.assertIn("name: Build\n", build)
        for text in (bump, build):
            self.assertEqual(text.count("  workflow_dispatch:"), 1)
            for trigger in ("  push:", "  pull_request:", "  workflow_run:", "  repository_dispatch:"):
                self.assertNotIn(trigger, text)
            for checkout in re.finditer(r"uses: actions/checkout@[^\n]+\n(.*?)(?=\n      -|\Z)",
                                        text, re.S):
                self.assertIn("ref: ${{ github.sha }}", checkout.group(1))
        self.assertNotIn("inputs:", build)
        self.assertEqual(bump.count("      mode:"), 1)
        for line in ("type: choice", "required: true", "default: patch",
                     "- patch", "- minor", "- major"):
            self.assertIn(line, bump)
        self.assertNotIn("release.py", bump)
        self.assertNotIn("bump.py", build)
        self.assertIn("group: hidapi-build-publication\n", build)
        self.assertIn("cancel-in-progress: false", build)
        self.assertNotIn("if: github.ref", build)

    def test_original_is_B_with_coordinated_header(self):
        bump = original(".github/workflows/bump-version.yml").decode()
        build = original(".github/workflows/release.yml").decode()
        self.assertIn("MARKETING_VERSION/CURRENT_PROJECT_VERSION AND", bump)
        self.assertIn('git tag "v${NEW}"', bump)
        self.assertIn("  push:\n    tags:", build)
        self.assertIn("      tag:", build)
        self.assertNotIn("if: github.ref", build)
        self.assertIn("build-windows:", build)
        self.assertIn("build-macos:", build)
        self.assertIn("needs: [version, build-windows]", build)

    def test_native_topology_and_command_preservation(self):
        current = (ROOT / ".github/workflows/release.yml").read_text()
        intake = original(".github/workflows/release.yml").decode()
        for line in (
            "    runs-on: windows-latest",
            "    runs-on: macos-latest",
            "    needs: [version, build-windows]",
            "  MACOS_DEPLOYMENT_TARGET: '10.13'",
            '          msbuild "%PRODUCT_NAME%.sln" /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0 /m',
            "        uses: microsoft/setup-msbuild@v2",
            "            -configuration Default \\",
            '            CONFIGURATION_BUILD_DIR="$PWD/$PLUGINS_DIR" \\',
            '            ARCHS="arm64 x86_64" \\',
            "            ONLY_ACTIVE_ARCH=NO \\",
            "            CODE_SIGNING_ALLOWED=NO \\",
            "          codesign --verbose --deep --timestamp --force --options=runtime \\",
            '          codesign --verify --deep --strict --verbose=2 "$BUNDLE_PATH"',
            '          xcrun stapler staple "$DMG_PATH"',
        ):
            self.assertIn(line, intake)
            self.assertIn(line, current)
        self.assertIn("lipo -verify_arch arm64 x86_64", current)
        self.assertNotIn("Remove-Item -Recurse", current)
        self.assertNotIn("rm -rf", current)
        self.assertNotIn("Select-Object -First", current)
        self.assertNotIn("find winbuild", current)
        self.assertNotIn("softprops/", current)
        preflight = current.index("      - name: authenticated publication preflight")
        self.assertLess(preflight, current.index("      - name: build x64"))
        preflight = current.index("      - name: authenticated macOS publication preflight")
        self.assertLess(preflight, current.index("      - name: import Developer ID certificate"))
        self.assertGreater(current.index("      - name: create new tag, release and exact assets"),
                           current.index('          xcrun stapler staple "$DMG_PATH"'))

    def test_only_version_tokens_change_in_native_project(self):
        path = "HIDAPI.xcodeproj/project.pbxproj"
        def scrub(data):
            return re.sub(rb"((?:MARKETING_VERSION|CURRENT_PROJECT_VERSION) = )[^;]+;",
                          rb"\g<1>VERSION;", data)
        self.assertEqual(scrub((ROOT / path).read_bytes()), scrub(original(path)))

    def test_nonversion_inputs_and_generated_destinations_unchanged(self):
        files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", INTAKE],
                                        cwd=ROOT, text=True).splitlines()
        excluded = {".github/workflows/bump-version.yml", ".github/workflows/release.yml",
                    "HIDAPI.xcodeproj/project.pbxproj", "English.lproj/InfoPlist.strings", "README.md",
                    # The ownership audit changes this helper, not the SDK or bundled binaries.
                    "support/4DPlugin-JSON.cpp"}
        for path in files:
            if path not in excluded:
                with self.subTest(path=path):
                    self.assertEqual((ROOT / path).read_bytes(), original(path))

    def test_source_localization_preserves_nonversion_bytes_and_CR(self):
        path = "English.lproj/InfoPlist.strings"
        def scrub(data):
            data = re.sub(rb'(CFBundleShortVersionString = ")[^"]+', rb"\g<1>VERSION", data)
            return re.sub(rb"(HIDAPI version )[^,]+", rb"\g<1>VERSION", data)
        current = (ROOT / path).read_bytes()
        self.assertEqual(scrub(current), scrub(original(path)))
        self.assertNotIn(b"\n", current)
        self.assertIn(b"\r", current)


if __name__ == "__main__":
    unittest.main()
