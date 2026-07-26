"""brix's native JSON dependency manifest, end to end on both engines.

The Makefile depfile is a Make-ism; brix also accepts its own JSON format
(`{"inputs": [...]}`) so any tool can declare its discovered inputs without
pretending to be `cc -MMD`. A discovered dep is fingerprint material, and the
Rust engine parses the manifest natively, so this asserts cross-engine parity:
an action that declares an input *only* through a JSON manifest still re-runs
when that input changes, and replays when it does not — identically on both
engines.
"""

import sys

import pytest

from brix import Graph, IncrementalBuilder, Reporter
from brix.actions import ExternalProcessAction
from brix.data import FileData, ToolData, ValueData

# A tiny tool: read the tracked data file, write it to the output, and emit a
# brix JSON manifest declaring that data file as a discovered input. The action
# does NOT declare the data file as an input up front — the manifest is the only
# thing that ties them together, so a re-run proves the manifest was consumed.
_TOOL = """\
import json, sys, pathlib
data, out, dep = (pathlib.Path(p) for p in sys.argv[1:4])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(data.read_text())
dep.parent.mkdir(parents=True, exist_ok=True)
dep.write_text(json.dumps({"version": 1, "inputs": [sys.argv[1]]}))
"""


def _graph() -> Graph:
    graph = Graph()
    graph.add(
        ExternalProcessAction(
            name="copy with manifest",
            command_line="{py} {tool} {data} {out} {dep}",
            inputs={
                "py": ToolData(sys.executable),
                "tool": FileData("tool.py"),  # the script, written into the workspace
                "data": ValueData("data/in.txt"),  # a bare arg, NOT a tracked input
            },
            outputs={"out": FileData("build/out.txt"), "dep": FileData("build/out.d.json")},
            depfile="build/out.d.json",
        )
    )
    return graph


class _Ran(Reporter):
    def __init__(self) -> None:
        self.ran: list[str] = []

    def process_started(self, node) -> None:
        self.ran.append(node.label)


def _build(root):
    r = _Ran()
    IncrementalBuilder(_graph, root=root, metadata_dir="build/.brix").build(reporter=r)
    return [x for x in r.ran if "copy with manifest" in x]


def test_json_manifest_folds_discovered_dep(tmp_path):
    (tmp_path / "tool.py").write_text(_TOOL)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/in.txt").write_text("one")

    assert _build(tmp_path)  # cold: runs
    assert not _build(tmp_path)  # unchanged: replays (manifest dep unchanged)

    (tmp_path / "data/in.txt").write_text("two")  # edit the discovered-only input
    assert _build(tmp_path)  # re-runs, because the JSON manifest declared it
    assert not _build(tmp_path)  # settles back to replay
