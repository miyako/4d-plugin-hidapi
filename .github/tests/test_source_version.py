"""Source-only parser regressions; no native tools or services."""

import importlib.util
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import source_version as source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".source-", dir=ROOT / ".github/tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for path in source.SOURCE_PATHS:
            dest = self.root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, dest)
        self.current = source.read(self.root)

    def snapshot(self):
        return {p: ((self.root / p).read_bytes(), stat.S_IMODE((self.root / p).stat().st_mode))
                for p in source.SOURCE_PATHS}

    def edit(self, path, transform):
        target = self.root / path
        target.write_bytes(transform(source.text(target)).encode())

    def plist(self, transform):
        path = self.root / source.PLIST
        data = plistlib.loads(path.read_bytes())
        transform(data)
        path.write_bytes(plistlib.dumps(data))

    def rejected(self):
        before = self.snapshot()
        with patch.object(Path, "write_bytes") as write:
            with self.assertRaises((ValueError, UnicodeError)):
                source.bump("patch", self.root)
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_live_policy_and_modes(self):
        self.assertEqual(source.SETTING_COUNT, 6)
        original = self.snapshot()
        old = list(map(int, self.current.split(".")))
        for mode, index in (("patch", 2), ("minor", 1), ("major", 0)):
            for path, (raw, _) in original.items():
                (self.root / path).write_bytes(raw)
            parts = old[:]
            parts[index] += 1
            parts[index + 1:] = [0] * (2 - index)
            expected = ".".join(map(str, parts))
            self.assertEqual(source.bump(mode, self.root), expected)
            self.assertEqual(source.read(self.root), expected)
            self.assertEqual(source.text(self.root / source.PROJECT).count(
                f"CURRENT_PROJECT_VERSION = {expected};"), 6)
            self.assertNotIn(source.BUILD, plistlib.loads((self.root / source.PLIST).read_bytes()))

    def test_missing_short_alone_and_both_are_zero_write(self):
        for with_build in (False, True):
            with self.subTest(with_build=with_build):
                self.plist(lambda d: (d.pop(source.SHORT, None),
                                     d.update({source.BUILD: "42"}) if with_build else None))
                self.rejected()

    def test_present_literal_build_and_all_current_settings_co_bump(self):
        self.plist(lambda d: d.update({source.BUILD: self.current, source.SHORT: self.current}))
        new = source.bump("minor", self.root)
        self.assertEqual(source.text(self.root / source.PROJECT).count(
            f"CURRENT_PROJECT_VERSION = {new};"), 6)
        self.assertEqual(plistlib.loads((self.root / source.PLIST).read_bytes())[source.BUILD], new)

    def test_references_preserved(self):
        self.edit(source.PROJECT, lambda s: s.replace(f"CURRENT_PROJECT_VERSION = {self.current};",
                                                     'CURRENT_PROJECT_VERSION = "${MARKETING_VERSION}";'))
        self.plist(lambda d: d.update({source.BUILD: "$(CURRENT_PROJECT_VERSION)"}))
        before = self.snapshot()
        new = source.bump("patch", self.root)
        for path, (raw, _) in before.items():
            self.assertEqual((self.root / path).read_bytes(),
                             raw.replace(self.current.encode(), new.encode()))

    def test_current_reference_short_resolves_and_remains_reference(self):
        self.plist(lambda d: d.update({source.SHORT: "$(CURRENT_PROJECT_VERSION)"}))
        new = source.bump("patch", self.root)
        self.assertEqual(source.read(self.root), new)
        self.assertEqual(plistlib.loads((self.root / source.PLIST).read_bytes())[source.SHORT],
                         "$(CURRENT_PROJECT_VERSION)")

    def test_stale_current_and_present_build_are_rejected_without_writes(self):
        project = self.root / source.PROJECT
        original = project.read_bytes()
        self.edit(source.PROJECT, lambda s: s.replace(
            f"CURRENT_PROJECT_VERSION = {self.current};", "CURRENT_PROJECT_VERSION = 1.0.0;"))
        self.rejected()
        project.write_bytes(original)
        self.plist(lambda d: d.update({source.BUILD: "52.3"}))
        self.rejected()

    def test_recursive_references_both_directions_and_cycles(self):
        project = self.root / source.PROJECT
        original = project.read_bytes()
        for key, target in (source.KEYS, tuple(reversed(source.KEYS))):
            project.write_bytes(original)
            self.edit(source.PROJECT, lambda s: s.replace(
                f"{key} = {self.current};", f'"{key}" = "$({target})";'))
            new = source.bump("patch", self.root)
            self.assertEqual(source.read(self.root), new)
            for path in source.SOURCE_PATHS:
                shutil.copy2(ROOT / path, self.root / path)
        self.edit(source.PROJECT, lambda s: s.replace(
            f"MARKETING_VERSION = {self.current};",
            'MARKETING_VERSION = "$(CURRENT_PROJECT_VERSION)";').replace(
                f"CURRENT_PROJECT_VERSION = {self.current};",
                'CURRENT_PROJECT_VERSION = "${MARKETING_VERSION}";'))
        self.rejected()

    def test_inline_quoted_duplicates_and_scoped_missing(self):
        path = self.root / source.PROJECT
        original = path.read_bytes()
        for key in source.KEYS:
            for quoted in (True, False):
                path.write_bytes(original)
                name = f'"{key}"' if quoted else key
                self.edit(source.PROJECT, lambda s: s.replace(
                    "buildSettings = {", f"buildSettings = {{ {name} = 1.0.0;", 1))
                self.rejected()
        path.write_bytes(original)
        self.edit(source.PROJECT, lambda s: s.replace("MARKETING_VERSION =", "OTHER =", 1))
        self.rejected()

    def test_quoted_inline_assignments_and_noise(self):
        self.edit(source.PROJECT, lambda s: s.replace(
            f"MARKETING_VERSION = {self.current};",
            f'OTHER = "MARKETING_VERSION = 9.9.9;"; "MARKETING_VERSION" = "{self.current}";'
        ) + "\n/* MARKETING_VERSION = 9.9.9; */\n// CURRENT_PROJECT_VERSION = 0;\n")
        source.bump("patch", self.root)
        value = source.text(self.root / source.PROJECT)
        self.assertIn('OTHER = "MARKETING_VERSION = 9.9.9;";', value)
        self.assertIn("/* MARKETING_VERSION = 9.9.9; */", value)

    def test_invalid_versions_and_current_drift(self):
        path = self.root / source.PROJECT
        original = path.read_bytes()
        for bad in ("01.2.3", "1.2", "1.2.3-beta", "$(UNKNOWN)", '"$(MARKETING_VERSION)"'):
            path.write_bytes(original)
            self.edit(source.PROJECT, lambda s: s.replace(
                f"MARKETING_VERSION = {self.current};", f"MARKETING_VERSION = {bad};"))
            self.rejected()
        path.write_bytes(original)
        self.edit(source.PROJECT, lambda s: s.replace(f"CURRENT_PROJECT_VERSION = {self.current};",
                                                     "CURRENT_PROJECT_VERSION = 2;", 1))
        self.rejected()

    def test_duplicate_plist_and_invalid_types(self):
        path = self.root / source.PLIST
        original = path.read_bytes()
        self.edit(source.PLIST, lambda s: s.replace("</dict>",
                  "<key>CFBundleShortVersionString</key><string>1.0.0</string></dict>"))
        self.rejected()
        for bad in (42, "$(UNKNOWN)", "0.0.0"):
            path.write_bytes(original)
            self.plist(lambda d: d.update({source.SHORT: bad}))
            self.rejected()

    def test_localization_duplicates_drift_and_malformed(self):
        path = self.root / source.LOCALIZED
        original = path.read_bytes()
        for suffix in (' "CFBundleShortVersionString" = "0.0.0";',
                       ' CFBundleName = "duplicate";', " /* unterminated", ' broken = "unterminated'):
            path.write_bytes(original + suffix.encode())
            self.rejected()
        path.write_bytes(original.replace(self.current.encode(), b"0.0.0"))
        self.rejected()

    def test_encoding_endings_bom_and_modes(self):
        original = self.snapshot()
        for encoding, bom in (("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
                              ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                              ("utf-16-le", b""), ("utf-16-be", b"")):
            for path, (raw, _) in original.items():
                value = source.decode(raw)[0]
                value = value.replace("\r\n", "\n").replace("\r", "\n")
                lines = value.splitlines(keepends=True)
                value = "".join(line.replace("\n", ("\r", "\n", "\r\n")[i % 3])
                                for i, line in enumerate(lines))
                if path == source.PLIST and encoding.startswith("utf-16"):
                    value = value.replace('encoding="UTF-8"', 'encoding="UTF-16"')
                (self.root / path).write_bytes(bom + value.encode(encoding))
                (self.root / path).chmod(0o640)
            before = self.snapshot()
            new = source.bump("patch", self.root)
            for path, (raw, mode) in before.items():
                self.assertEqual((self.root / path).read_bytes(),
                                 raw.replace(self.current.encode(encoding), new.encode(encoding)))
                self.assertEqual(stat.S_IMODE((self.root / path).stat().st_mode), mode)

    def test_xml_comments_and_unrelated_versions(self):
        noise = f"<!-- <key>CFBundleVersion</key><string>{self.current}</string> -->"
        self.edit(source.PLIST, lambda s: s.replace("</dict>", noise + "</dict>"))
        self.edit(source.LOCALIZED, lambda s: s + f'Unrelated = "{self.current}";')
        source.bump("patch", self.root)
        self.assertIn(noise, source.text(self.root / source.PLIST))
        self.assertIn(f'Unrelated = "{self.current}";', source.text(self.root / source.LOCALIZED))

    def test_missing_source_and_invalid_encoding_never_write(self):
        path = self.root / source.LOCALIZED
        raw = path.read_bytes()
        path.unlink()
        with patch.object(Path, "write_bytes") as write, self.assertRaises(FileNotFoundError):
            source.bump("patch", self.root)
        write.assert_not_called()
        path.write_bytes(b"\0\0ambiguous\0\0")
        self.rejected()
        path.write_bytes(raw)

    def test_cli_inspection_and_invalid_mode_never_write(self):
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", str(ROOT / ".github/scripts/source_version.py")],
                                cwd=self.root, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), self.current)
        with self.assertRaises(ValueError):
            source.bump("invalid", self.root)
        self.assertEqual(self.snapshot(), before)

    def test_built_formats_and_optional_absence(self):
        bundle = self.root / "built.bundle"
        contents = bundle / "Contents"
        contents.mkdir(parents=True)
        dest = contents / "Resources/English.lproj/InfoPlist.strings"
        dest.parent.mkdir(parents=True)
        fields = {source.SHORT: self.current}
        localized = {key: item[0] for key, item in source.inspect(self.root)[4].items()}
        for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            (contents / "Info.plist").write_bytes(plistlib.dumps(fields, fmt=fmt))
            dest.write_bytes(plistlib.dumps(localized, fmt=fmt))
            source.verify_bundle(bundle, self.root)
        dest.write_bytes(source.text(self.root / source.LOCALIZED).encode("utf-16"))
        source.verify_bundle(bundle, self.root)
        fields[source.BUILD] = "1.0.0"
        (contents / "Info.plist").write_bytes(plistlib.dumps(fields))
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            source.verify_bundle(bundle, self.root)


if __name__ == "__main__":
    unittest.main()
