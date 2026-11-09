#!/usr/bin/env python3
"""Invalidate exact generated outputs without removing packaged resources."""

import argparse
from pathlib import Path
import shutil


BUNDLE = Path("test/Plugins/HIDAPI.bundle")
MAC_FILES = (
    BUNDLE / "Contents/MacOS/HIDAPI",
    BUNDLE / "Contents/Info.plist",
    BUNDLE / "Contents/Resources/English.lproj/InfoPlist.strings",
    BUNDLE / "Contents/CodeResources",
)
MAC_DIRECTORIES = (
    BUNDLE / "Contents/_CodeSignature",
    Path("build/HIDAPI.build"),
    Path("build/XCBuildData"),
    Path("test/Plugins/HIDAPI.build"),
)
WINDOWS_FILES = (BUNDLE / "Contents/Windows64/HIDAPI.4DX",)
WINDOWS_DIRECTORIES = (Path("Objs/Release"),)


def prepare(platform):
    root = Path.cwd().resolve()
    files, directories = (
        (MAC_FILES, MAC_DIRECTORIES) if platform == "macos"
        else (WINDOWS_FILES, WINDOWS_DIRECTORIES)
    )
    for path in (*files, *directories):
        for parent in (path, *path.parents):
            if parent == Path("."):
                break
            if parent.is_symlink():
                raise ValueError(f"Refusing symlink output: {parent}")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Output escapes checkout: {path}")
    for path in files:
        path.unlink(missing_ok=True)
    for path in directories:
        if path.exists():
            shutil.rmtree(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("macos", "windows"))
    prepare(parser.parse_args().platform)
