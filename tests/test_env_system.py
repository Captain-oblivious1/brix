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

from brix import Graph, IncrementalBuilder, SYSTEM, UNSET, build_stack
from brix.env import PASS
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


def test_unset_is_fingerprint_material(tmp_path):
    # UNSET must also enter the *fingerprint*, not just action_key: the cache
    # layer keys on fingerprint alone, so if the two actions shared one, adding
    # UNSET would replay a cached artifact produced WITH the variable visible
    # (proven stale before the fix — the phase-3 Fable review's finding).
    from brix.incremental import action_fingerprint
    from brix.layers import NodeContext
    from brix.metadata import TimeAndHashChangeDetector

    (tmp_path / "a.c").write_text("int main;")
    ctx = NodeContext.for_root(tmp_path)
    det = TimeAndHashChangeDetector(tmp_path)
    without = action_fingerprint(_action(), ctx, det)
    with_unset = action_fingerprint(_action(env={"X": UNSET}), ctx, det)
    assert without != with_unset


UNSET_CACHE_TOOL = """\
import os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(os.environ.get("BRIX_TEST_ENVVAR", "<absent>"))
"""


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_unset_flip_misses_the_cache(tmp_path, monkeypatch, engine):
    # End-to-end pin of the same hole across the wire: an artifact cached
    # without UNSET (ambient var visible to the tool) must NOT be served once
    # the action declares the var UNSET — the declared "I must not be affected
    # by X" would be silently defeated by a fingerprint-keyed cache hit.
    monkeypatch.setenv("BRIX_ENGINE", engine)
    monkeypatch.setenv("BRIX_TEST_ENVVAR", "ambient-value")
    cache = tmp_path / "cache"

    def graph_for(env):
        def supplier():
            graph = Graph()
            d = graph.data
            graph.add(
                ExternalProcessAction(
                    name="probe",
                    command_line="{python} {tool} {dst}",
                    inputs={"python": d.tool(sys.executable), "tool": d.file("tool.py")},
                    outputs={"dst": d.file("out.txt")},
                    env=env,
                )
            )
            return graph
        return supplier

    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    (ws1 / "tool.py").write_text(UNSET_CACHE_TOOL)
    builder = build_stack(graph_for(None), root=ws1, cache_dir=cache, staging_dir=None)
    assert builder.build().success
    assert (ws1 / "out.txt").read_text() == "ambient-value"

    ws2 = tmp_path / "ws2"  # fresh workspace: only the cache could answer
    ws2.mkdir()
    (ws2 / "tool.py").write_text(UNSET_CACHE_TOOL)
    builder = build_stack(
        graph_for({"BRIX_TEST_ENVVAR": UNSET}), root=ws2, cache_dir=cache, staging_dir=None
    )
    result = builder.build()
    assert result.success
    assert (ws2 / "out.txt").read_text() == "<absent>"
    assert all(r.source == "process" for r in result.process_results)


# --- the worker set-vs-track asymmetry ---------------------------------------

def test_worker_routed_action_rejects_pinned_env():
    # A warm worker's environment is fixed at launch: a literal pin or an UNSET
    # can never reach it, so silently accepting one would fingerprint an
    # environment the process never saw (the guard-bypass class). It must fail
    # loudly at construction; SYSTEM/PASS (fingerprint/scrub-side only) stay legal.
    from brix import WorkerSpec

    spec = WorkerSpec(name="w", command_line="run {jar}", inputs={"jar": FileData("w.jar")})

    def worker_action(env):
        return ExternalProcessAction(
            name="compile",
            command_line="--sources {src} -d {out}",
            inputs={"src": FileData("a.java")},
            outputs={"out": FileData("classes")},
            worker=spec,
            env=env,
        )

    with pytest.raises(ValueError, match="fixed at launch"):
        worker_action({"LANG": "C"})
    with pytest.raises(ValueError, match="fixed at launch"):
        worker_action({"JAVA_TOOL_OPTIONS": UNSET})
    # Tracking is fingerprint-only and letting-through is scrub-only: both legal.
    action = worker_action({"JAVA_HOME": SYSTEM, "PATH": PASS})
    assert action.env_used == ("JAVA_HOME",)
    assert action.env_pass == ("PATH",)


def test_base_env_matches_the_rust_engine():
    # brix.env.BASE_ENV (Python scrub) and process.rs BASE_ENV (Rust scrub) must
    # stay in sync, or the two engines would hand a scrubbed process different
    # plumbing. Pinned by scraping the constant out of the Rust source — the
    # cheap cross-language parity check.
    import re
    from pathlib import Path

    from brix.env import BASE_ENV

    source = (
        Path(__file__).resolve().parents[1] / "rust/brix-core/src/process.rs"
    ).read_text()
    match = re.search(r"pub const BASE_ENV: &\[&str\] = &\[([^\]]*)\];", source)
    assert match is not None, "BASE_ENV constant not found in process.rs"
    rust_names = re.findall(r'"([^"]+)"', match.group(1))
    assert sorted(BASE_ENV) == sorted(rust_names)
    assert len(rust_names) == len(set(rust_names))


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


# --- PASS: present under scrub, never fingerprinted --------------------------

def test_pass_collects_into_env_pass_and_is_not_fingerprinted():
    action = _action(env={"PATH": PASS, "TRACKED": SYSTEM})
    assert action.env_pass == ("PATH",)     # let through under scrub
    assert "PATH" not in action.env         # not a literal set
    assert "PATH" not in action.env_used    # not tracked
    # PASS is deliberately absent from the action identity (its value must not
    # enter the cache key — that would break cross-machine caching).
    assert action_key(_action(env={"PATH": PASS})) == action_key(_action())


# --- scrub: authoritative environment, on both engines ----------------------

_SCRUB_PROBE = """\
import os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(
    "undeclared=" + os.environ.get("BRIX_SCRUB_UNDECLARED", "<absent>")
    + " tracked=" + os.environ.get("BRIX_SCRUB_TRACKED", "<absent>")
    + " path=" + ("yes" if os.environ.get("PATH") else "no")
)
"""


def _scrub_graph():
    graph = Graph()
    d = graph.data
    graph.add(
        ExternalProcessAction(
            name="probe",
            command_line="{python} {tool} {dst}",
            inputs={"python": d.tool(sys.executable), "tool": d.file("probe.py")},
            outputs={"dst": d.file("out.txt")},
            env={"BRIX_SCRUB_TRACKED": SYSTEM},
        )
    )
    return graph


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_scrub_gives_an_authoritative_environment(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("BRIX_ENGINE", engine)
    monkeypatch.setenv("BRIX_SCRUB_UNDECLARED", "leaked")   # undeclared ambient
    monkeypatch.setenv("BRIX_SCRUB_TRACKED", "tracked")     # declared SYSTEM
    (tmp_path / "probe.py").write_text(_SCRUB_PROBE)

    builder = build_stack(
        _scrub_graph, root=tmp_path, cache_dir=None, staging_dir=None, scrub=True
    )
    assert builder.build().success
    out = (tmp_path / "out.txt").read_text()
    # Undeclared var scrubbed; declared var and PATH (base plumbing) present.
    assert "undeclared=<absent>" in out
    assert "tracked=tracked" in out
    assert "path=yes" in out


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_without_scrub_undeclared_vars_leak_through(tmp_path, monkeypatch, engine):
    # The default (additive) behavior — the counterpart that makes the scrub
    # guarantee meaningful: with scrub off, the undeclared var is visible.
    monkeypatch.setenv("BRIX_ENGINE", engine)
    monkeypatch.setenv("BRIX_SCRUB_UNDECLARED", "leaked")
    monkeypatch.setenv("BRIX_SCRUB_TRACKED", "tracked")
    (tmp_path / "probe.py").write_text(_SCRUB_PROBE)

    builder = build_stack(
        _scrub_graph, root=tmp_path, cache_dir=None, staging_dir=None  # scrub off
    )
    assert builder.build().success
    assert "undeclared=leaked" in (tmp_path / "out.txt").read_text()


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
