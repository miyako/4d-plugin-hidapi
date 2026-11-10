"""Compile actual helpers and the entire unchanged SDK; mock only host services."""

import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def compile_source(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)


@unittest.skipUnless(platform.system() == "Darwin", "native CoreFoundation harness requires macOS")
class StringOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="hidapi-ownership-")
        cls.addClassCleanup(cls.temp.cleanup)
        out = Path(cls.temp.name)
        # A detached immutable tree can be supplied without copying or editing SDK code.
        source = Path(os.environ.get("HIDAPI_OWNERSHIP_SOURCE", ROOT))
        flags = ["-g", "-O1", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                 "-I", str(source / "4D Plugin API"),
                 "-I", str(source / "4D Plugin API/Classes"), "-I", str(source / "support")]
        # Legacy SDK initializes an integer with NULL; leave vendor source unchanged.
        compile_source(["xcrun", "clang", "-Wno-int-conversion", *flags, "-c",
                        str(source / "4D Plugin API/4DPluginAPI.c"),
                        "-o", str(out / "sdk.o")])
        cls.exe = out / "ownership"
        compile_source(["xcrun", "clang++", "-std=c++11", *flags, "-x", "objective-c++",
                        str(ROOT / ".github/tests/string_ownership.cpp"),
                        str(source / "support/4DPlugin-JSON.cpp"),
                        str(source / "4D Plugin API/Classes/ARRAY_TEXT.cpp"),
                        str(source / "4D Plugin API/Classes/C_TEXT.cpp"),
                        "-x", "none", str(out / "sdk.o"),
                        "-framework", "Cocoa", "-framework", "CoreFoundation",
                        "-framework", "CoreGraphics", "-o", str(cls.exe)])

    def test_string_setters(self):
        for overload in range(4):
            self.run_modes(f"setters{overload}")

    def test_property_getters(self):
        self.run_modes("getters")

    def test_array_and_content_contracts(self):
        self.run_modes("contracts")

    def run_modes(self, mode):
        for empty in ("null", "allocated"):
            with self.subTest(empty=empty):
                result = subprocess.run([str(self.exe), mode, empty], text=True,
                                        capture_output=True,
                                        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0",
                                             "UBSAN_OPTIONS": "halt_on_error=1"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
