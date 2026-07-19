"""brix.rust — wrap cargo.

The lowering-shape and manifest-inference tests need no toolchain; a gated test
then builds a real crate to confirm the wrapper actually drives cargo. The
adapter reads Cargo.toml (the adopt-the-manifest pattern), so Cargo.toml stays
the single source of truth.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from brix import Graph, IncrementalBuilder, rust
from brix.data import FileData, GlobData, ValueData

REPO = Path(__file__).resolve().parents[1]


def test_cargo_action_lowers_to_a_tracked_cargo_build():
    action = rust.CargoBuildAction(
        manifest="crate/Cargo.toml",
        artifact="target/release/libfoo.a",
        sources=GlobData("crate/{name}.rs"),
    )
    (proc,) = action.lower()
    assert proc.name == "cargo build -> target/release/libfoo.a"
    # cargo self-resolves sources/target; only the manifest is a command arg.
    assert proc.command_line == "{cargo} build --manifest-path {manifest} {options}"
    assert "sources" in proc.inputs            # tracked, not on the command line
    assert proc.outputs["artifact"].path == "target/release/libfoo.a"
    assert "--release" in proc.inputs["options"].text


def test_env_and_env_used_are_threaded():
    action = rust.CargoBuildAction(
        manifest="Cargo.toml",
        artifact="target/release/foo",
        sources=GlobData("{name}.rs"),
        env={"CARGO_NET_OFFLINE": "true"},
        env_used=["RUSTFLAGS"],
    )
    (proc,) = action.lower()
    assert "CARGO_NET_OFFLINE" in proc.env            # user literal wins over the default
    assert "RUSTFLAGS" in proc.env_used               # tracked (alongside the cargo defaults)


def test_adapter_infers_self_host_artifact_from_the_real_manifest():
    # Point the adapter at brix's own engine manifest: a cdylib named brix_py in
    # the rust/ workspace -> rust/target/release/libbrix_py.so. This is the
    # self-hosting wiring (brix building brix), proven without a build.
    action = rust.cargo("rust/brix-py/Cargo.toml", root=REPO)
    (proc,) = action.lower()
    assert proc.outputs["artifact"].path == "rust/target/release/libbrix_py.so"
    # Conservative footprint: the whole workspace's .rs, plus manifests + lock.
    globs = {a.pattern.template for a in proc.inputs["sources"].atoms()
             if isinstance(a, GlobData)}
    files = {a.path for a in proc.inputs["sources"].atoms() if isinstance(a, FileData)}
    assert "rust/{name}.rs" in globs
    assert "rust/Cargo.lock" in files


def test_adapter_infers_staticlib_and_binary(tmp_path):
    # staticlib crate-type -> lib<name>.a
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "foo-lib"\nversion = "0.1.0"\nedition = "2021"\n'
        '[lib]\ncrate-type = ["staticlib"]\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("")
    lib = rust.cargo("Cargo.toml", root=tmp_path)
    assert lib.lower()[0].outputs["artifact"].path == "target/release/libfoo_lib.a"

    # a src/main.rs -> the binary, named for the package
    (tmp_path / "src/main.rs").write_text("fn main() {}")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "foo-bin"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    exe = rust.cargo("Cargo.toml", root=tmp_path)
    assert exe.lower()[0].outputs["artifact"].path == "target/release/foo-bin"


needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="no cargo")


@needs_cargo
def test_cargo_builds_a_real_crate(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "greet"\nversion = "0.1.0"\nedition = "2021"\n'
        '[lib]\ncrate-type = ["staticlib"]\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text(
        textwrap.dedent(
            """\
            #[no_mangle]
            pub extern "C" fn greet() -> i32 { 42 }
            """
        )
    )

    # Settle Cargo.lock up front so it is a stable tracked input from build one.
    subprocess.run(["cargo", "generate-lockfile"], cwd=tmp_path, check=True)

    def create_graph():
        g = Graph()
        g.add(rust.cargo("Cargo.toml", root=tmp_path))
        return g

    # Plain incremental stack (cargo writes its own target/ — not atomic-stageable).
    result = IncrementalBuilder(create_graph, root=tmp_path).build()
    assert result.success, result.errors
    assert (tmp_path / "target/release/libgreet.a").is_file()

    # Nothing changed: the cargo action replays (crate-level skip).
    second = IncrementalBuilder(create_graph, root=tmp_path).build()
    assert all(r.source == "replayed" for r in second.process_results)


needs_ffi = pytest.mark.skipif(
    shutil.which("cargo") is None or shutil.which("cc") is None, reason="no cargo/cc"
)


@needs_ffi
def test_rust_staticlib_links_into_c_and_relinks_on_edit(tmp_path):
    # The cross-language edge: a Rust staticlib linked into a C program. Editing
    # the Rust source must rerun cargo AND relink the C app, while the unchanged
    # C compile replays. Modeled on examples/rust_ffi/.
    from brix import cpp

    example = REPO / "examples" / "rust_ffi"
    shutil.copytree(example / "crate", tmp_path / "crate",
                    ignore=shutil.ignore_patterns("target"))
    (tmp_path / "src").mkdir()
    for f in ("main.c", "greet.h"):
        shutil.copy(example / "src" / f, tmp_path / "src" / f)
    subprocess.run(["cargo", "generate-lockfile"], cwd=tmp_path / "crate", check=True)

    def create_graph():
        g = Graph()
        d = g.data
        g.add(
            rust.cargo("crate/Cargo.toml", root=tmp_path),
            cpp.CompileSourceAction(
                source_files=d.glob("src/{name}.c"),
                object_files=d.glob("build/obj/{name}.o"),
                compiler=d.tool("cc"),
            ),
            cpp.LinkApplicationAction(
                object_files=d.glob("build/obj/{name}.o")
                + d.file("crate/target/release/libgreet.a"),
                output_file=d.file("build/app"),
                linker=d.tool("cc"),
                link_options=["-lpthread", "-ldl", "-lm"],
            ),
        )
        return g

    assert IncrementalBuilder(create_graph, root=tmp_path).build().success
    app = tmp_path / "build/app"
    assert "answer is 42" in subprocess.run([str(app)], capture_output=True, text=True).stdout

    # Edit the Rust source: cargo + link rerun; the C compile replays.
    lib = tmp_path / "crate/src/lib.rs"
    lib.write_text(lib.read_text().replace("42", "7"))
    result = IncrementalBuilder(create_graph, root=tmp_path).build()
    ran = {r.action_name for r in result.process_results if r.source == "process"}
    assert any("cargo build" in n for n in ran)
    assert any("link" in n for n in ran)
    assert not any("compile main" in n for n in ran)  # C unchanged -> replayed
    assert "answer is 7" in subprocess.run([str(app)], capture_output=True, text=True).stdout
