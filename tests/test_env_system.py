"""`SYSTEM` sentinel — env-dict tracking that desugars to `env_used`.

`env={"X": SYSTEM}` means "take X from the ambient environment and fingerprint
it" — the phase-3 vocabulary's inherit-and-track intent. It is implemented as
pure desugaring into the phase-2 `env_used` mechanism (already Fable-reviewed and
cross-engine), so `env={"X": SYSTEM}` must produce **byte-identical** fingerprint
material to `env_used=["X"]`. These tests pin that equivalence (the safety
argument for shipping it without a fresh fingerprint-path review), the env vs.
env_used split, and the behavioral re-stale on both engines.
"""

import sys

import pytest

from brix import Graph, IncrementalBuilder, SYSTEM, UNSET
from brix.actions import _ENV_USED_KEY, ExternalProcessAction
from brix.data import FileData, ValueData
from brix.incremental import action_key


def _action(*, env=None, env_used=None):
    return ExternalProcessAction(
        name="compile",
        command_line="{cc} -o {out} {src}",
        inputs={"cc": ValueData("gcc"), "src": FileData("a.c")},
        outputs={"out": FileData("a.o")},
        env=env,
        env_used=env_used,
    )


def test_system_is_equivalent_to_env_used(monkeypatch):
    # The core safety claim: env={"X": SYSTEM} materializes the identical
    # fingerprint-only input as env_used=["X"] (the blessed mechanism).
    monkeypatch.setenv("MYVAR", "value-1")
    via_system = _action(env={"MYVAR": SYSTEM})
    via_env_used = _action(env_used=["MYVAR"])
    assert via_system.env_used == via_env_used.env_used == ("MYVAR",)
    assert (
        via_system.inputs[_ENV_USED_KEY].text
        == via_env_used.inputs[_ENV_USED_KEY].text
    )


def test_system_tracks_but_does_not_set(monkeypatch):
    monkeypatch.setenv("MYVAR", "v")
    action = _action(env={"MYVAR": SYSTEM})
    assert "MYVAR" in action.env_used          # tracked
    assert action.env == {}                    # not set as process env
    assert _ENV_USED_KEY not in action.command_line


def test_literal_and_system_split_correctly(monkeypatch):
    monkeypatch.setenv("TRACKED", "t")
    action = _action(env={"PINNED": "literal-value", "TRACKED": SYSTEM})
    # literal -> set (fingerprinted as an env value); SYSTEM -> tracked only.
    assert set(action.env) == {"PINNED"}
    assert action.env["PINNED"].text == "literal-value"
    assert action.env_used == ("TRACKED",)


def test_system_folds_in_with_explicit_env_used(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    action = _action(env={"A": SYSTEM}, env_used=["B"])
    assert set(action.env_used) == {"A", "B"}   # order-preserving union, deduped


# --- UNSET: force a variable absent -----------------------------------------

def test_unset_collects_into_env_unset_not_env():
    action = _action(env={"SECRET": UNSET, "PINNED": "v"})
    assert action.env_unset == ("SECRET",)     # forced absent
    assert set(action.env) == {"PINNED"}        # literal still set
    assert "SECRET" not in action.env


def test_unset_is_part_of_the_action_identity():
    # Flipping a var between UNSET and any other intent must re-stale (change the
    # action_key), since it changes what the process sees.
    without = action_key(_action())
    with_unset = action_key(_action(env={"X": UNSET}))
    assert without != with_unset


TOOL_PROBE = """\
import os, sys
from pathlib import Path
Path("runs.log").open("a").write("ran\\n")
Path(sys.argv[1]).write_text("[" + os.environ.get("BRIX_TEST_ENVVAR", "") + "]")
"""


def _probe_graph():
    graph = Graph()
    d = graph.data
    graph.add(
        ExternalProcessAction(
            name="probe",
            command_line="{python} {tool} {dst}",
            inputs={"python": d.tool(sys.executable), "tool": d.file("tool.py")},
            outputs={"dst": d.file("out.txt")},
            env={"BRIX_TEST_ENVVAR": UNSET},
        )
    )
    return graph


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_unset_removes_an_ambient_variable_from_the_process(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("BRIX_ENGINE", engine)
    monkeypatch.setenv("BRIX_TEST_ENVVAR", "leaked")  # set in the ambient env
    (tmp_path / "tool.py").write_text(TOOL_PROBE)
    assert IncrementalBuilder(_probe_graph, root=tmp_path).build().success
    # UNSET removed it, so the tool saw it absent — not "leaked".
    assert (tmp_path / "out.txt").read_text() == "[]"


# --- behavioral: SYSTEM re-stales exactly like env_used, on both engines ----

TOOL = """\
import sys
from pathlib import Path
Path("runs.log").open("a").write("ran\\n")
Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text())
"""


def _build_graph():
    graph = Graph()
    d = graph.data
    graph.add(
        ExternalProcessAction(
            name="copy",
            command_line="{python} {tool} {src} {dst}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tool.py"),
                "src": d.file("src.txt"),
            },
            outputs={"dst": d.file("out.txt")},
            env={"BRIX_TEST_ENVVAR": SYSTEM},
        )
    )
    return graph


def _runs(root):
    log = root / "runs.log"
    return log.read_text().count("ran") if log.exists() else 0


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_system_change_rebuilds_unchanged_replays(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("BRIX_ENGINE", engine)
    (tmp_path / "tool.py").write_text(TOOL)
    (tmp_path / "src.txt").write_text("hello")

    monkeypatch.setenv("BRIX_TEST_ENVVAR", "before")
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 1

    # Unchanged environment -> replay.
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 1

    # Tracked variable changes -> re-run, though no file did.
    monkeypatch.setenv("BRIX_TEST_ENVVAR", "after")
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 2
