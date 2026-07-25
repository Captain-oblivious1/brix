"""Per-language default env maps — the phase-3 overlays (java, rust).

Each language module tracks its toolchain's behaviour-affecting environment by
default (via SYSTEM), so a change to e.g. JAVA_TOOL_OPTIONS or RUSTFLAGS
re-stales the build automatically — the user never has to know the trap. This is
the "auto-close silent drift" payoff, and it lands even with scrub off. cpp's
overlay is deferred (its actions appear in the goldens, so it needs a golden
regeneration) and covered separately.

The tracked vars are unset in a normal test environment, so they record as
absent (null) and cause no spurious rebuilds — asserted here.
"""

import shutil

import pytest

from brix import Graph, IncrementalBuilder, cpp, java, rust
from brix.actions import _ENV_USED_KEY

data = Graph().data


def _env_used_of(action, step_name_contains):
    """The env_used names on a lowered action's step matching a name fragment."""
    for step in action.lower():
        # unwrap ExpandAll children if present
        target = getattr(step, "child", step)
        if step_name_contains in (target.name or ""):
            return set(target.env_used)
    raise AssertionError(f"no step matching {step_name_contains!r}")


def test_jaraction_tracks_jvm_behaviour_vars():
    action = java.JarAction(
        source_files=data.glob("src/{name}.java"),
        output_file=data.file("app.jar"),
    )
    tracked = _env_used_of(action, "javac")
    assert {"JAVA_HOME", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS"} <= tracked


def test_cpp_compile_and_link_track_toolchain_vars():
    compile_action = cpp.CompileSourceAction(
        source_file=data.glob("src/{name}.cpp"),
        object_file=data.glob("build/obj/{name}.o"),
    )
    tracked = _env_used_of(compile_action, "compile")
    assert {"CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
            "LIBRARY_PATH", "SOURCE_DATE_EPOCH"} <= tracked

    link_action = cpp.LinkApplicationAction(
        object_files=data.glob("build/obj/{name}.o"),
        output_file=data.file("build/app"),
    )
    assert {"CPATH", "LIBRARY_PATH", "SOURCE_DATE_EPOCH"} <= _env_used_of(link_action, "link")


def test_cargo_tracks_rustc_behaviour_vars():
    action = rust.CargoBuildAction(
        manifest="Cargo.toml",
        artifact="target/release/libfoo.a",
        sources=data.glob("{name}.rs"),
    )
    tracked = _env_used_of(action, "cargo build")
    assert {"RUSTFLAGS", "RUSTUP_TOOLCHAIN", "CARGO_HOME", "CARGO_NET_OFFLINE"} <= tracked


def test_user_env_overrides_the_cargo_default():
    # A user pinning a default var wins; it moves from tracked to set.
    action = rust.CargoBuildAction(
        manifest="Cargo.toml",
        artifact="target/release/libfoo.a",
        sources=data.glob("{name}.rs"),
        env={"RUSTFLAGS": "-C opt-level=0"},
    )
    (proc,) = action.lower()
    assert "RUSTFLAGS" not in proc.env_used          # user pinned it -> not tracked
    assert proc.env["RUSTFLAGS"].text == "-C opt-level=0"


def test_defaults_materialize_the_env_used_input():
    action = java.JarAction(
        source_files=data.glob("src/{name}.java"),
        output_file=data.file("app.jar"),
    )
    (compile_step,) = [s for s in action.lower() if "javac" in (s.name or "")]
    assert _ENV_USED_KEY in compile_step.inputs   # the snapshot is a tracked input


needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="no cargo")


@needs_cargo
def test_unset_default_vars_do_not_cause_spurious_rebuilds(tmp_path):
    # RUSTFLAGS etc. are unset -> null snapshot -> stable -> a no-op still replays,
    # so the default tracking is free when the vars are not in play.
    import subprocess

    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n'
        '[lib]\ncrate-type = ["staticlib"]\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("")
    subprocess.run(["cargo", "generate-lockfile"], cwd=tmp_path, check=True)

    def create_graph():
        g = Graph()
        g.add(rust.cargo("Cargo.toml", root=tmp_path))
        return g

    assert IncrementalBuilder(create_graph, root=tmp_path).build().success
    second = IncrementalBuilder(create_graph, root=tmp_path).build()
    assert all(r.source == "replayed" for r in second.process_results)
