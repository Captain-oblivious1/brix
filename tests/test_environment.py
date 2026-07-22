"""Environment recording (phase 1: diagnostic capture).

The "works on my machine" detective: capture the environment each build runs
under, and let `brix_build.py env` diff current-vs-recorded. It records and shows —
it never rebuilds, fingerprints, or splits a cache — so these tests cover the
filter, the record round-trip, the diff classification, and the CLI wiring
(capture on build, exit codes, the status note, clean removal).
"""

import os

from brix import cli, environment
from brix.environment import DEFAULT_IGNORE, capture, diff, read_record, write_record

from test_incremental import make_graph, write_workspace


# --- the noise filter ------------------------------------------------------

def test_capture_drops_session_noise_keeps_signal():
    fake = {
        "PWD": "/somewhere",       # noise
        "SHLVL": "3",              # noise
        "_": "/usr/bin/python",    # noise
        "TERM": "xterm-256color",  # noise
        "SSH_AUTH_SOCK": "/run/x", # noise (SSH_* glob)
        "PATH": "/usr/bin",        # signal
        "HOME": "/home/duane",     # signal
        "JAVA_HOME": "/opt/jdk",   # signal
        "CFLAGS": "-O2",           # signal
    }
    kept = capture(environ=fake)
    for noise in ("PWD", "SHLVL", "_", "TERM", "SSH_AUTH_SOCK"):
        assert noise not in kept
    for signal in ("PATH", "HOME", "JAVA_HOME", "CFLAGS"):
        assert signal in kept


def test_capture_ignore_extends_defaults_not_replaces():
    fake = {"PWD": "/x", "MY_TOKEN": "secret", "PATH": "/usr/bin"}
    kept = capture(environ=fake, ignore=["MY_TOKEN"])
    assert "MY_TOKEN" not in kept   # project override honored
    assert "PWD" not in kept        # default ignore still applies
    assert "PATH" in kept           # signal survives


def test_capture_ignore_supports_globs():
    fake = {"CI_JOB_1": "a", "CI_JOB_2": "b", "PATH": "/usr/bin"}
    kept = capture(environ=fake, ignore=["CI_JOB_*"])
    assert "CI_JOB_1" not in kept and "CI_JOB_2" not in kept
    assert "PATH" in kept


# --- record round-trip and diff -------------------------------------------

def test_write_and_read_record(tmp_path):
    write_record(tmp_path, command="build", variables={"PATH": "/usr/bin", "CC": "gcc"})
    record = read_record(tmp_path)
    assert record["brix_command"] == "build"
    assert record["variables"] == {"CC": "gcc", "PATH": "/usr/bin"}
    assert record["captured_at"].endswith("Z")


def test_read_missing_record_is_none(tmp_path):
    assert read_record(tmp_path) is None


def test_read_corrupt_record_is_none(tmp_path):
    (tmp_path / "environment.json").write_text("{not json")
    assert read_record(tmp_path) is None


def test_diff_classifies_added_removed_changed():
    recorded = {"A": "1", "B": "2", "C": "3"}
    current = {"A": "1", "B": "changed", "D": "new"}
    d = diff(recorded, current)
    assert d.changed == {"B": ("2", "changed")}
    assert d.added == {"D": "new"}
    assert d.removed == {"C": "3"}
    assert d.count == 3
    assert not d.is_empty


def test_diff_empty_when_identical():
    same = {"A": "1", "B": "2"}
    assert diff(same, same).is_empty


# --- CLI wiring ------------------------------------------------------------

def _args(tmp_path):
    return {"root": tmp_path, "metadata_dir": "build/.brix"}


def test_env_before_any_build_is_clean(tmp_path, capsys):
    assert cli.main(make_graph, argv=["env"], **_args(tmp_path)) == 0
    assert "No build environment recorded yet" in capsys.readouterr().out


def test_build_captures_then_env_reports_unchanged(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    assert cli.main(make_graph, argv=["build", "--silent"], **_args(tmp_path)) == 0
    capsys.readouterr()
    # Same process, same environment -> no changes, exit 0.
    assert cli.main(make_graph, argv=["env"], **_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "variable(s) recorded" in out
    assert "No changes since then" in out


def test_env_reports_change_with_exit_1(tmp_path, capsys, monkeypatch):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    monkeypatch.setenv("BRIX_ENV_PROBE", "before")
    assert cli.main(make_graph, argv=["build", "--silent"], **_args(tmp_path)) == 0
    capsys.readouterr()

    monkeypatch.setenv("BRIX_ENV_PROBE", "after")  # mutate after the build
    assert cli.main(make_graph, argv=["env"], **_args(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "BRIX_ENV_PROBE" in out
    assert "before" in out and "after" in out


def test_env_ignore_suppresses_a_noisy_var(tmp_path, capsys, monkeypatch):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    monkeypatch.setenv("CI_BUILD_TOKEN", "one")
    args = {**_args(tmp_path), "env_ignore": ["CI_BUILD_TOKEN"]}
    assert cli.main(make_graph, argv=["build", "--silent"], **args) == 0
    capsys.readouterr()

    monkeypatch.setenv("CI_BUILD_TOKEN", "two")  # would diff, but it is ignored
    assert cli.main(make_graph, argv=["env"], **args) == 0
    assert "No changes since then" in capsys.readouterr().out


def test_status_note_appears_when_environment_differs(tmp_path, capsys, monkeypatch):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    monkeypatch.setenv("BRIX_ENV_PROBE", "before")
    assert cli.main(make_graph, argv=["build", "--silent"], **_args(tmp_path)) == 0
    capsys.readouterr()

    monkeypatch.setenv("BRIX_ENV_PROBE", "after")
    # status stays clean (exit 0) but surfaces the environment note.
    code = cli.main(make_graph, argv=["status"], **_args(tmp_path))
    out = capsys.readouterr().out
    assert code == 0  # graph is fresh; the note does not change that
    assert "environment:" in out and "run 'brix_build.py env'" in out


def test_failed_build_still_captures_environment(tmp_path, capsys):
    # A build that fails is equally worth diffing later.
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    (tmp_path / "tools/upper.py").write_text("import sys; sys.exit(3)\n")  # break it
    assert cli.main(make_graph, argv=["build", "--silent"], **_args(tmp_path)) == 1
    capsys.readouterr()
    assert read_record(tmp_path / "build/.brix") is not None
