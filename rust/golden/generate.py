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
    "build/classes/{**rel}.class",  # recursive binding: matches across /
    "src/{**pkg}/{cls}.java",  # recursive then single segment
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
    "build/classes/App.class",  # recursive, zero extra segments
    "build/classes/com/foo/Bar.class",  # recursive, nested
    "src/com/foo/Bar.java",  # recursive {**pkg} + single {cls}
]

SUBSTITUTIONS = [
    {"name": "foo"},
    {"a": "X", "b": "Y"},
    {"x": "1", "y": "2"},
    {"video": "intro", "scene": "s1"},
    {},  # unknown placeholders left intact
    {"a": "only-a"},  # partial
    {"rel": "com/foo/Bar"},  # recursive binding value spans /
    {"pkg": "com/foo", "cls": "Bar"},
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


# -- planner + targets: spec -> plan.describe() ------------------------------


def atom_to_spec(atom):
    from brix.data import FileData, GlobData, RegexData, ToolData, ValueData

    if isinstance(atom, FileData):
        d = {"kind": "file", "path": atom.path}
        if atom.identity is not None:
            d["identity"] = atom.identity
        return d
    if isinstance(atom, GlobData):
        d = {"kind": "glob", "template": atom.pattern.template}
        if atom.identity is not None:
            d["identity"] = atom.identity
        return d
    if isinstance(atom, RegexData):
        return {"kind": "regex", "pattern": atom.regex.pattern}
    if isinstance(atom, ToolData):
        return {"kind": "tool", "name": atom.name}
    if isinstance(atom, ValueData):
        return {"kind": "value", "text": atom.text}
    raise TypeError(atom)


def expr_to_spec(expr):
    return [atom_to_spec(a) for a in expr.atoms()]


def action_to_spec(action):
    from brix.actions import DeleteFilesAction, ExpandAll, ExternalProcessAction

    if isinstance(action, ExternalProcessAction):
        d = {
            "kind": "process",
            "command_line": action.command_line,
            "inputs": {k: expr_to_spec(v) for k, v in action.inputs.items()},
            "outputs": {k: expr_to_spec(v) for k, v in action.outputs.items()},
        }
        if action.name is not None:
            d["name"] = action.name
        if action.env:
            d["env"] = {k: expr_to_spec(v) for k, v in action.env.items()}
        return d
    if isinstance(action, ExpandAll):
        return {
            "kind": "expand",
            "element": action.element_name,
            "collection": expr_to_spec(action.collection),
            "child": action_to_spec(action.child),
        }
    if isinstance(action, DeleteFilesAction):
        return {"kind": "delete", "paths": list(action.paths)}
    raise TypeError(action)


def graph_to_spec(graph):
    return {"version": 1, "actions": [action_to_spec(a) for a in graph.actions]}


def graph_upper():
    from brix import ExpandAll, ExternalProcessAction, Graph

    g = Graph()
    d = g.data
    g.add(
        ExpandAll(
            "name",
            d.glob("src/{name}.txt"),
            ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": d.tool("python3"),
                    "tool": d.file("tools/upper.py"),
                    "source": d.file("src/{name}.txt"),
                },
                outputs={"dest": d.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": d.tool("python3"),
                "tool": d.file("tools/concat.py"),
                "parts": d.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": d.file("build/all.txt")},
        ),
    )
    return g


def graph_cpp():
    from brix import Graph, cpp

    g = Graph()
    d = g.data
    g.add(
        cpp.CompileSourceAction(
            source_files=d.glob("src/{name}.cpp"),
            object_files=d.glob("build/obj/{name}.o"),
        ),
        cpp.LinkApplicationAction(
            object_files=d.glob("build/obj/{name}.o"),
            output_file=d.file("build/bin/app"),
        ),
    )
    return g


def graph_stem():
    # The make shortest-stem case: {video}.mpg also matches wealth.raw.mpg as
    # video=wealth.raw, so the {video}.raw.mpg producer (stem wealth) must win.
    from brix import ExternalProcessAction, Graph

    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="raw {video}",
            command_line="mk {src} {out}",
            inputs={"src": d.file("{video}.src")},
            outputs={"out": d.file("{video}.raw.mpg")},
        ),
        ExternalProcessAction(
            name="final {video}",
            command_line="mk {rawf} {out}",
            inputs={"rawf": d.file("{video}.raw.mpg")},
            outputs={"out": d.file("{video}.mpg")},
        ),
    )
    return g


def graph_regex():
    # A concrete producer feeds a regex collection, so dependency inference must
    # go through the regex-vs-concrete overlap path.
    from brix import ExpandAll, ExternalProcessAction, Graph

    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="gen",
            command_line="gen {out}",
            outputs={"out": d.file("videos/01.py")},
        ),
        ExpandAll(
            "scene",
            d.regex(r"videos/(?P<scene>[0-9][^/]*)\.py"),
            ExternalProcessAction(
                name="render {scene}",
                command_line="mk {src} {o}",
                inputs={"src": d.file("videos/{scene}.py")},
                outputs={"o": d.file("build/{scene}.out")},
            ),
        ),
    )
    return g


PLAN_CASES = [
    ("upper", graph_upper, [None, ["build/all.txt"], ["build/upper/foo.txt"]]),
    ("cpp", graph_cpp, [None, ["build/bin/app"]]),
    ("stem", graph_stem, [None, ["wealth.mpg"]]),
    ("regex", graph_regex, [None]),
]


def gen_plans():
    from brix.planner import plan_graph

    out = []
    for name, factory, target_sets in PLAN_CASES:
        graph = factory()
        # Store the spec as a raw JSON *string* (key order preserved) so the
        # Rust side deserializes it directly; round-tripping through a generic
        # JSON value would sort object keys and break edge-reason ordering.
        spec_json = json.dumps(graph_to_spec(graph), ensure_ascii=False)
        for targets in target_sets:
            plan = plan_graph(graph, Path("/root"), targets)
            out.append(
                {"name": name, "spec": spec_json, "targets": targets, "describe": plan.describe()}
            )
    return out


# -- fingerprint keys (structural, content-independent) ----------------------


def gen_keys():
    from brix.actions import ExpandAll, ExternalProcessAction
    from brix.incremental import action_key, membership_key

    out = []

    def collect(action):
        if isinstance(action, ExternalProcessAction):
            out.append(
                {
                    "kind": "action_key",
                    "spec": json.dumps(action_to_spec(action), ensure_ascii=False),
                    "key": action_key(action),
                }
            )
        elif isinstance(action, ExpandAll):
            out.append(
                {
                    "kind": "membership_key",
                    "spec": json.dumps(action_to_spec(action), ensure_ascii=False),
                    "key": membership_key(action),
                }
            )
            collect(action.child)

    for _, factory, _ in PLAN_CASES:
        for action in factory().actions:
            collect(action)
    return out


# -- RegexData match + substitute --------------------------------------------


def gen_regex():
    from brix.data import RegexData

    match_cases = []
    for pattern, paths in [
        (
            r"videos/(?P<video>[^/]+)/(?P<scene>[0-9][^/]*)\.py",
            ["videos/alpha/01.py", "videos/alpha/__init__.py", "videos/a/b/c.py", "videos/alpha/2x.py"],
        ),
        (r"(?P<name>[^/]+)\.txt", ["foo.txt", "a/b.txt", "foo.md"]),
    ]:
        r = RegexData(pattern)
        for path in paths:
            match_cases.append({"pattern": pattern, "path": path, "bindings": r.match(path)})

    subst_cases = []
    for pattern, binds in [
        (r"videos/(?P<video>[^/]+)/(?P<scene>[0-9][^/]*)\.py", {"video": "alpha"}),
        (r"videos/(?P<video>[^/]+)/(?P<scene>[0-9][^/]*)\.py", {"video": "a.b-c"}),  # re.escape
        (r"(?P<name>[^/]+)\.txt", {}),  # unchanged
    ]:
        result = RegexData(pattern).substitute(binds).regex.pattern
        subst_cases.append({"pattern": pattern, "bindings": binds, "result": result})

    return {"match": match_cases, "substitute": subst_cases}


# -- content fingerprint: a committed fixture tree + filesystem realization --
#
# `find` and `hash_file` (and `action_fingerprint`, part 2b) read real files.
# The tree is written here and committed, so the Rust tests read the identical
# bytes: content hashing is sha256-of-bytes, which is mtime-independent, so a
# git checkout (which resets mtimes) reproduces the same digests. Fixtures use
# only file/glob/regex/value atoms — no tool (PATH/mtime), identity (plugin), or
# worker — so every value is fully determined by the tree's bytes and layout.

TREE_ROOT = ROOT / "rust" / "golden" / "tree"

TREE = {
    "src/a.txt": "alpha\n",
    "src/b.txt": "beta\n",
    "src/nested/c.txt": "gamma\n",
    "src/notes.md": "notes\n",
    "tools/upper.py": "print('x')\n",
}


def write_tree():
    for rel, content in TREE.items():
        path = TREE_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


FIND_TEMPLATES = [
    "src/a.txt",  # concrete, present
    "src/missing.txt",  # concrete, absent
    "src/{name}.txt",  # single-segment binding: a, b (not nested/c, not .md)
    "src/{a}/{b}.txt",  # multi-segment: nested/c
    "src/{name}.md",  # notes
    "nope/{x}.txt",  # base dir absent
]

HASH_PATHS = [
    "src/a.txt",
    "src/nested/c.txt",
    "src",  # a directory -> None (not a regular file)
    "src/missing.txt",  # absent -> None
]


def gen_fs():
    from brix.metadata import TimeAndHashChangeDetector
    from brix.patterns import PathPattern

    det = TimeAndHashChangeDetector(TREE_ROOT)  # metadata is never saved to disk
    find_cases = []
    for tmpl in FIND_TEMPLATES:
        results = PathPattern(tmpl).find(TREE_ROOT)
        find_cases.append(
            {"template": tmpl, "results": [[rel, binds] for rel, binds in results]}
        )
    hash_cases = [
        {"path": rel, "hash": det.hash_file(TREE_ROOT / rel)} for rel in HASH_PATHS
    ]
    return {"find": find_cases, "hash_file": hash_cases}


# -- content fingerprint: action_fingerprint over the fixture tree -----------
#
# Only file/glob/regex/value/union atoms and env — no tool/identity/worker — so
# the fingerprint is fully determined by the tree's bytes (the Rust port defers
# those three, erroring rather than guessing). `outputs` are present on the
# actions but never enter the fingerprint.


def gen_fingerprints():
    from brix import ExternalProcessAction, Graph
    from brix.incremental import action_fingerprint
    from brix.layers import NodeContext
    from brix.metadata import TimeAndHashChangeDetector

    d = Graph().data
    ctx = NodeContext.for_root(TREE_ROOT)
    det = TimeAndHashChangeDetector(TREE_ROOT)  # metadata is never saved to disk

    actions = [
        ExternalProcessAction(
            name="one file",
            command_line="cat {src} {out}",
            inputs={"src": d.file("src/a.txt")},
            outputs={"out": d.file("build/out.txt")},
        ),
        ExternalProcessAction(
            name="glob",
            command_line="cat {srcs} {out}",
            inputs={"srcs": d.glob("src/{name}.txt")},
            outputs={"out": d.file("build/all.txt")},
        ),
        ExternalProcessAction(
            name="regex over iter_files",
            command_line="cat {srcs} {out}",
            inputs={"srcs": d.regex(r"src/[^/]+\.txt")},
            outputs={"out": d.file("build/all.txt")},
        ),
        ExternalProcessAction(
            name="value and env",
            command_line="cc {flag} {src} {out}",
            inputs={"src": d.file("src/a.txt"), "flag": d.value("-O2")},
            outputs={"out": d.file("build/a.o")},
            env={"CC": d.value("clang")},
        ),
        ExternalProcessAction(
            name="union input",
            command_line="pack {all} {out}",
            inputs={"all": d.file("tools/upper.py") + d.glob("src/{name}.txt")},
            outputs={"out": d.file("build/pack.txt")},
        ),
    ]
    return [
        {
            "spec": json.dumps(action_to_spec(a), ensure_ascii=False),
            "fingerprint": action_fingerprint(a, ctx, det),
        }
        for a in actions
    ]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    write_tree()
    for name, data in [
        ("digests", gen_digests()),
        ("patterns", gen_patterns()),
        ("overlaps", gen_overlaps()),
        ("shlex", gen_shlex()),
        ("plans", gen_plans()),
        ("keys", gen_keys()),
        ("regex", gen_regex()),
        ("fs", gen_fs()),
        ("fingerprints", gen_fingerprints()),
    ]:
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(ROOT)} ({len(data)} cases)")


if __name__ == "__main__":
    main()
