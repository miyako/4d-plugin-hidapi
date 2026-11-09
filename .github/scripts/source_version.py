"""Coordinated source versions; copied bundles and 4D counters are not sources."""

import argparse
from pathlib import Path
import plistlib
import re
from xml.parsers.expat import ExpatError


PROJECT = Path("HIDAPI.xcodeproj/project.pbxproj")
PLIST = Path("Info.plist")
LOCALIZED = Path("English.lproj/InfoPlist.strings")
SOURCE_PATHS = (PROJECT, PLIST, LOCALIZED)
KEYS = ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION")
SETTING_COUNT = 6
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
BUILD_VERSION = VERSION
REFERENCES = {f"$({key})": key for key in KEYS}
REFERENCES.update({f"${{{key}}}": key for key in KEYS})
SHORT = "CFBundleShortVersionString"
BUILD = "CFBundleVersion"
INFO = "CFBundleGetInfoString"
TOKEN = re.compile(
    r'\s+|/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|'
    r'\$\([^()\s]*\)|\$\{[^{}\s]*\}|[{}=;(),]|[^\s"{}=;(),/]+|/',
    re.DOTALL,
)


class UniqueDictionary(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError(f"Duplicate metadata key: {key}")
        super().__setitem__(key, value)


def decode(data):
    for bom, encoding in (
        (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding), encoding, bom
    if b"\0" in data:
        even, odd = data[::2].count(0), data[1::2].count(0)
        if len(data) % 2 or not (odd > 2 * even or even > 2 * odd):
            raise ValueError("Malformed or ambiguous BOM-less source encoding")
        encoding = "utf-16-le" if odd > 2 * even else "utf-16-be"
        value = data.decode(encoding)
        if "\0" in value:
            raise ValueError("NUL characters are unsupported in metadata")
        return value, encoding, b""
    return data.decode("utf-8"), "utf-8", b""


def text(path):
    return decode(Path(path).read_bytes())[0]


def unquote(value):
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


def tokens(value):
    result = []
    offset = 0
    while offset < len(value):
        if value.startswith("/*", offset) and "*/" not in value[offset + 2:]:
            raise ValueError("Unterminated metadata comment")
        match = TOKEN.match(value, offset)
        if match is None:
            raise ValueError("Unsupported or unterminated metadata serialization")
        raw = match[0]
        if not raw.isspace() and not raw.startswith(("/*", "//")):
            quoted = raw.startswith('"')
            result.append((unquote(raw), match.start() + quoted, match.end() - quoted, quoted))
        offset = match.end()
    return result


def assignments(value, keys, count=None):
    ts = tokens(value)
    fields = {key: [] for key in keys}
    scopes = [set()]
    for index, (key, start, end, quoted) in enumerate(ts):
        if not quoted and key == "{":
            scopes.append(set())
        elif not quoted and key == "}":
            if len(scopes) == 1:
                raise ValueError("Unbalanced metadata dictionary")
            scopes.pop()
        elif key in fields and index + 1 < len(ts) and ts[index + 1][0] == "=":
            if index + 3 >= len(ts) or ts[index + 3][0] != ";":
                raise ValueError(f"Malformed assignment for {key}")
            if key in scopes[-1]:
                raise ValueError(f"Duplicate assignment for {key}")
            scopes[-1].add(key)
            fields[key].append(ts[index + 2])
    if len(scopes) != 1:
        raise ValueError("Unbalanced metadata dictionary")
    if count is not None and any(len(values) != count for values in fields.values()):
        raise ValueError(f"Expected {count} source assignments for each version key")
    return fields


def load_plist(data):
    try:
        fmt = plistlib.FMT_BINARY if data.startswith(b"bplist00") else plistlib.FMT_XML
        value = plistlib.loads(data, fmt=fmt, dict_type=UniqueDictionary)
    except (plistlib.InvalidFileException, ExpatError, UnicodeError) as exc:
        raise ValueError("Invalid plist serialization") from exc
    if not isinstance(value, dict):
        raise ValueError("Expected plist dictionary")
    return value


def resolved(value, values):
    if not isinstance(value, str):
        raise ValueError("Version metadata must be strings")
    return values.get(REFERENCES.get(value), value)


def localized_fields(value):
    # Consume the complete strings file, including unrelated entries, so malformed
    # trailing data and duplicate quoted/unquoted keys cannot hide from validation.
    ts = tokens(value)
    fields = UniqueDictionary()
    if len(ts) % 4:
        raise ValueError("Unsupported strings serialization")
    for index in range(0, len(ts), 4):
        key, equals, item, semi = ts[index:index + 4]
        if equals[0] != "=" or semi[0] != ";" or not item[3]:
            raise ValueError("Unsupported strings serialization")
        fields[key[0]] = item
    return fields


def inspect_data(data):
    decoded = {path: decode(data[path])[0] for path in SOURCE_PATHS}
    settings = assignments(decoded[PROJECT], KEYS, SETTING_COUNT)
    values = {}

    def resolve(key, visiting=()):
        if key in visiting:
            raise ValueError("Cyclic source version reference")
        if key not in values:
            candidates = set()
            for value, _, _, _ in settings[key]:
                if value in REFERENCES:
                    value = resolve(REFERENCES[value], visiting + (key,))
                elif not re.fullmatch(VERSION, value):
                    raise ValueError(f"{key} must resolve to canonical X.Y.Z")
                candidates.add(value)
            if len(candidates) != 1:
                raise ValueError(f"{key} has drifted across configurations")
            values[key] = candidates.pop()
        return values[key]

    if len({resolve(key) for key in KEYS}) != 1:
        raise ValueError("MARKETING_VERSION and CURRENT_PROJECT_VERSION have drifted")
    version = values[KEYS[0]]
    plist = load_plist(data[PLIST])
    if SHORT not in plist:
        raise ValueError("CFBundleShortVersionString is required")
    edits = {path: [] for path in SOURCE_PATHS}
    xml = re.sub(r"<!--.*?-->", lambda match: " " * len(match[0]),
                 decoded[PLIST], flags=re.DOTALL)
    for key in (SHORT, BUILD):
        if key not in plist:
            continue
        value = resolved(plist[key], values)
        if value != version:
            raise ValueError(f"{key} is invalid, unresolved or drifted")
        matches = list(re.finditer(
            r"<key>" + key + r"</key>\s*<string>([^<]*)</string>", xml,
        ))
        if len(matches) != 1 or matches[0][1] != plist[key]:
            raise ValueError(f"{key} has unsupported XML serialization")
        if plist[key] not in REFERENCES:
            edits[PLIST].append(matches[0].span(1))
    for fields in settings.values():
        for value, start, end, _ in fields:
            if value not in REFERENCES:
                edits[PROJECT].append((start, end))
    localized = localized_fields(decoded[LOCALIZED])
    if SHORT not in localized or resolved(localized[SHORT][0], values) != version:
        raise ValueError("Localized short version is missing or drifted")
    expected_info = f"HIDAPI version {version}, Copyright 2020 miyako."
    if INFO not in localized or localized[INFO][0] != expected_info:
        raise ValueError("Localized information version is missing or drifted")
    short, start, end, _ = localized[SHORT]
    if short not in REFERENCES:
        edits[LOCALIZED].append((start, end))
    _, start, _, _ = localized[INFO]
    start += len("HIDAPI version ")
    edits[LOCALIZED].append((start, start + len(version)))
    if BUILD in localized:
        value, start, end, _ = localized[BUILD]
        if resolved(value, values) != version:
            raise ValueError("Localized build version is invalid or drifted")
        if value not in REFERENCES:
            edits[LOCALIZED].append((start, end))
    return version, edits, values, plist, localized


def inspect(root=Path(".")):
    root = Path(root)
    return inspect_data({path: (root / path).read_bytes() for path in SOURCE_PATHS})


def read(root=Path(".")):
    return inspect(root)[0]


def bump(mode, root=Path(".")):
    index = ("major", "minor", "patch").index(mode)
    root = Path(root)
    data = {path: (root / path).read_bytes() for path in SOURCE_PATHS}
    current, edits, _, _, _ = inspect_data(data)
    parts = list(map(int, current.split(".")))
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    new = ".".join(map(str, parts))
    updates = {}
    for path in SOURCE_PATHS:
        value, encoding, bom = decode(data[path])
        for start, end in sorted(edits[path], reverse=True):
            value = value[:start] + new + value[end:]
        updates[path] = bom + value.encode(encoding)
    if inspect_data(updates)[0] != new:
        raise ValueError("Bumped metadata did not validate")
    for path, content in updates.items():
        if content != data[path]:
            (root / path).write_bytes(content)
    return new


def verify_bundle(bundle, root=Path(".")):
    _, _, values, source, localized = inspect(root)
    contents = Path(bundle) / "Contents"
    built = load_plist((contents / "Info.plist").read_bytes())
    for key in (SHORT, BUILD):
        if key in source:
            if built.get(key) != resolved(source[key], values):
                raise ValueError(f"Built {key} differs from selected source")
        elif key in built:
            raise ValueError(f"Unexpected built {key}")
    raw = (contents / "Resources/English.lproj/InfoPlist.strings").read_bytes()
    if raw.startswith(b"bplist00") or decode(raw)[0].lstrip().startswith("<?xml"):
        actual = load_plist(raw)
    else:
        actual = {key: item[0] for key, item in localized_fields(decode(raw)[0]).items()}
    for key in (SHORT, BUILD, INFO):
        if key not in localized:
            if key in actual:
                raise ValueError(f"Unexpected built localized {key}")
        else:
            expected = localized[key][0] if key == INFO else resolved(localized[key][0], values)
            if actual.get(key) != expected:
                raise ValueError(f"Built localized {key} differs from selected source")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--bump", choices=("patch", "minor", "major"))
    operation.add_argument("--verify-bundle", type=Path)
    args = parser.parse_args()
    if args.verify_bundle:
        verify_bundle(args.verify_bundle)
    else:
        print(bump(args.bump) if args.bump else read())
