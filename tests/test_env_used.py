"""`env_used=` — environment as fingerprint material (phase 2: the guard).

Phase 1 (`brix.py env`) *records* the environment. Phase 2 makes a declared
subset a real dependency: `env_used=["JAVA_HOME"]` snapshots those variables
into a fingerprint-only input, so a change re-stales the action — but it never
*sets* them (unlike `env=`) and never reaches the command line. The snapshot is
resolved on the Python side into an ordinary `ValueData`, so both engines
fingerprint it identically (pinned mechanically by the `env_used` golden case in
rust/golden/generate.py). See docs/environment-recording.md.
"""

import sys

import pytest

from brix import Graph, IncrementalBuilder, Reporter
from brix.actions import _ENV_USED_KEY, ExternalProcessAction
from brix.data import FileData, ValueData


# --- materialization / isolation (no build) --------------------------------

def _action(env_used):
    return ExternalProcessAction(
        name="compile",
        command_line="{cc} -o {out} {src}",
        inputs={"cc": ValueData("gcc"), "src": FileData("a.c")},
        outputs={"out": FileData("a.o")},
        env_used=env_used,
    )


def test_env_used_materializes_a_reserved_fingerprint_only_input(monkeypatch):
    monkeypatch.setenv("MYVAR", "one")
    action = _action(["MYVAR"])
    assert _ENV_USED_KEY in action.inputs
    assert '"MYVAR": "one"' in action.inputs[_ENV_USED_KEY].text
    # Never referenced by the command, never set as process env.
    assert _ENV_USED_KEY not in action.command_line
    assert action.env == {}


def test_no_env_used_adds_nothing():
    action = _action(None)
    assert _ENV_USED_KEY not in action.inputs
    assert action.env_used == ()


def test_unset_variable_is_distinct_from_empty(monkeypatch):
    monkeypatch.delenv("MYVAR", raising=False)
    unset = _action(["MYVAR"]).inputs[_ENV_USED_KEY].text
    monkeypatch.setenv("MYVAR", "")
    empty = _action(["MYVAR"]).inputs[_ENV_USED_KEY].text
    assert unset != empty
    assert '"MYVAR": null' in unset
    assert '"MYVAR": ""' in empty


def test_substitute_preserves_the_snapshot_verbatim(monkeypatch):
    # An env value containing a `{binding}`-shaped substring must survive
    # expansion unrewritten (copied verbatim, not placeholder-substituted).
    monkeypatch.setenv("MYVAR", "/opt/{name}/jdk")
    action = _action(["MYVAR"])
    narrowed = action.substitute({"name": "X"})
    assert narrowed.inputs[_ENV_USED_KEY].text == action.inputs[_ENV_USED_KEY].text
    assert "/opt/{name}/jdk" in narrowed.inputs[_ENV_USED_KEY].text
    assert narrowed.env_used == ("MYVAR",)


def test_reserved_input_key_cannot_be_supplied_directly(monkeypatch):
    # An author-supplied `_brix_env_used` would silently replace the live
    # snapshot with a constant — a guard bypass in the stale direction. It is
    # rejected loudly, with or without env_used=.
    monkeypatch.setenv("MYVAR", "one")
    for env_used in (["MYVAR"], None):
        with pytest.raises(ValueError, match="reserved"):
            ExternalProcessAction(
                name="forged",
                command_line="cc {src} {out}",
                inputs={"src": FileData("a.c"), _ENV_USED_KEY: ValueData("stale")},
                outputs={"out": FileData("a.o")},
                env_used=env_used,
            )


# --- fingerprint sensitivity -----------------------------------------------

def _fingerprint(env_value, monkeypatch):
    from pathlib import Path

    from brix.incremental import action_fingerprint

    if env_value is None:
        monkeypatch.delenv("MYVAR", raising=False)
    else:
        monkeypatch.setenv("MYVAR", env_value)

    class Det:
        metadata = None
        def stat_signature(self, p): return None
        def hash_file(self, p): return "h"

    class Ctx:
        workspace_root = Path(".")
        def read_path(self, p): return Path(p)

    return action_fingerprint(_action(["MYVAR"]), Ctx(), Det())


def test_changing_the_variable_changes_the_fingerprint(monkeypatch):
    assert _fingerprint("one", monkeypatch) != _fingerprint("two", monkeypatch)


def test_same_value_same_fingerprint(monkeypatch):
    assert _fingerprint("one", monkeypatch) == _fingerprint("one", monkeypatch)


def test_unset_fingerprint_distinct(monkeypatch):
    assert _fingerprint(None, monkeypatch) != _fingerprint("one", monkeypatch)


# --- end-to-end: the guard, on a real incremental build --------------------

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
            env_used=["BRIX_TEST_ENVVAR"],
        )
    )
    return graph


def _runs(root):
    log = root / "runs.log"
    return log.read_text().count("ran") if log.exists() else 0


def _prepare(tmp_path):
    (tmp_path / "tool.py").write_text(TOOL)
    (tmp_path / "src.txt").write_text("hello")


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_env_change_rebuilds_unchanged_replays(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("BRIX_ENGINE", engine)
    _prepare(tmp_path)

    monkeypatch.setenv("BRIX_TEST_ENVVAR", "before")
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 1

    # Same environment -> fingerprint fresh -> the action replays (no re-run).
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 1

    # The tracked variable changes -> the action re-runs, though no file did.
    monkeypatch.setenv("BRIX_TEST_ENVVAR", "after")
    assert IncrementalBuilder(_build_graph, root=tmp_path).build().success
    assert _runs(tmp_path) == 2


def test_env_used_does_not_set_the_process_environment(tmp_path, monkeypatch):
    # A tool must NOT see an env_used variable unless it was already ambient:
    # env_used tracks, it does not inject (that is what env= is for).
    monkeypatch.delenv("BRIX_TEST_ENVVAR", raising=False)
    probe_tool = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(os.environ.get('BRIX_TEST_ENVVAR', '<unset>'))\n"
    )
    (tmp_path / "probe.py").write_text(probe_tool)

    def graph():
        g = Graph()
        d = g.data
        g.add(
            ExternalProcessAction(
                name="probe",
                command_line="{python} {tool} {out}",
                inputs={"python": d.tool(sys.executable), "tool": d.file("probe.py")},
                outputs={"out": d.file("seen.txt")},
                env_used=["BRIX_TEST_ENVVAR"],
            )
        )
        return g

    monkeypatch.setenv("BRIX_TEST_ENVVAR", "tracked-but-not-injected")
    assert IncrementalBuilder(graph, root=tmp_path).build().success
    # The variable WAS ambient here, so the process sees it — but via
    # inheritance, not via env_used. Prove env_used didn't add/override it:
    # unset it ambiently and the process must see <unset> despite env_used.
    monkeypatch.delenv("BRIX_TEST_ENVVAR", raising=False)
    assert IncrementalBuilder(graph, root=tmp_path).build().success
    assert (tmp_path / "seen.txt").read_text() == "<unset>"
