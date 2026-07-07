#!/usr/bin/env python3
"""Generate cross-language golden fixtures from the REAL brix Python code.

The Rust `brix-core` must reproduce these byte-for-byte (digests) and
behaviour-for-behaviour (patterns, overlaps, shlex). Running the actual Python
functions here — not a re-description of them — is what makes the Rust port's
"matches Python" claim verifiable.

Writes JSON into rust/brix-core/tests/golden/. Re-run whenever the Python
fingerprint/pattern/shlex semantics change (and then re-run `cargo test`).

    python3 rust/golden/generate.py
"""

import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from brix.incremental import _digest  # noqa: E402
from brix.patterns import PathPattern, overlaps  # noqa: E402

OUT = ROOT / "rust" / "brix-core" / "tests" / "golden"


# -- digest payloads: exercise the canonical JSON + sha256 -------------------

DIGEST_PAYLOADS = [
    "hello",
    "",
    123,
    True,
    False,
    None,
    ["file", "src/a.txt"],
    {"b": 2, "a": 1, "c": [1, 2, 3]},  # key sorting
    {"command": "{cc} -c {src} -o {obj}", "inputs": {"src": [["file", "a.c"]]}},
    # unicode: BMP + astral, must escape as \uXXXX / surrogate pair
    "café",
    "中文",
    "emoji 😀 tail",
    {"π": "value", "a": "é"},  # non-ASCII keys sort by code point
    # control characters and JSON metacharacters inside strings
    "line1\nline2\ttab",
    "null\x00byte",
    "ctrl\x1fend",
    "del\x7fchar",  # 0x7f is ASCII, passes through literally
    'quote " and backslash \\ slash /',
    # nested structure resembling a real action fingerprint payload
    {
        "command": "{python} {tool} {source} {dest}",
        "inputs": {
            "python": [["tool", "/usr/bin/python3", ["mtime", 123]]],
            "source": [["file", "src/a.txt", "deadbeef"]],
        },
        "env": {"FLAG": [["value", "on"]]},
    },
]


def gen_digests():
    return [{"payload": p, "digest": _digest(p)} for p in DIGEST_PAYLOADS]


# -- patterns: match / substitute / literal_prefix_dir / is_concrete ---------

PATTERN_TEMPLATES = [
    "src/{name}.txt",
    "build/upper/{name}.txt",
    "a/b.txt",  # concrete
    "{a}/{a}.txt",  # repeated binding
    "{a}-{b}.txt",  # adjacent bindings, needs backtracking
    "a/{x}/b/{y}.txt",  # multiple segments
    "{video}/{scene}.png",
    "prefix{n}suffix",
]

MATCH_PATHS = [
    "src/foo.txt",
    "src/a/b.txt",
    "a/b.txt",
    "x/x.txt",
    "x/y.txt",
    "one-two.txt",
    "a/mid/b/end.txt",
    "intro/scene1.png",
    "prefixMIDsuffix",
    "nomatch",
]

SUBSTITUTIONS = [
    {"name": "foo"},
    {"a": "X", "b": "Y"},
    {"x": "1", "y": "2"},
    {"video": "intro", "scene": "s1"},
    {},  # unknown placeholders left intact
    {"a": "only-a"},  # partial
]


def gen_patterns():
    out = []
    for tmpl in PATTERN_TEMPLATES:
        p = PathPattern(tmpl)
        matches = []
        for path in MATCH_PATHS:
            m = p.match(path)
            matches.append({"path": path, "bindings": m})  # dict or None
        subs = []
        for binds in SUBSTITUTIONS:
            subs.append({"bindings": binds, "result": p.substitute(binds)})
        out.append(
            {
                "template": tmpl,
                "is_concrete": p.is_concrete,
                "literal_prefix_dir": p.literal_prefix_dir,
                "matches": matches,
                "substitutions": subs,
            }
        )
    return out


# -- overlaps ----------------------------------------------------------------

OVERLAP_PAIRS = [
    ("a/b.txt", "a/b.txt"),
    ("a/b.txt", "a/c.txt"),
    ("src/{name}.txt", "src/foo.txt"),
    ("src/{name}.txt", "other/foo.txt"),
    ("src/{name}.txt", "src/{other}.txt"),
    ("build/{n}.o", "build/{m}.c"),
    ("{video}.mpg", "wealth.raw.mpg"),
    ("a/{x}/b.txt", "a/{y}/c.txt"),
    ("a/{x}/b.txt", "a/{y}/b.txt"),
]


def gen_overlaps():
    out = []
    for a, b in OVERLAP_PAIRS:
        out.append({"a": a, "b": b, "overlaps": overlaps(PathPattern(a), PathPattern(b))})
    return out


# -- shlex -------------------------------------------------------------------

SHLEX_INPUTS = [
    "a b c",
    "  spaced   out ",
    "",
    "   ",
    "'single quoted with spaces' next",
    "a'b'c",
    'double "quoted value" end',
    r'esc "a\"b\\c" end',
    r"back\ slash\ escaped",
    "{python} tools/upper.py 'src/a b.txt' build/out.txt",
    "'unterminated",
    '"unterminated',
]


def gen_shlex():
    out = []
    for s in SHLEX_INPUTS:
        try:
            out.append({"input": s, "argv": shlex.split(s)})
        except ValueError:
            out.append({"input": s, "error": True})
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in [
        ("digests", gen_digests()),
        ("patterns", gen_patterns()),
        ("overlaps", gen_overlaps()),
        ("shlex", gen_shlex()),
    ]:
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(ROOT)} ({len(data)} cases)")


if __name__ == "__main__":
    main()
