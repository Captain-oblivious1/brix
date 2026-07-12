"""Typed TypeScript actions: the `ts.CompileAction` lowering and the ts-dts lens
surface (`ts.dts` / `DtsFile` / `DtsGlob`). The end-to-end compile and the
impl-only-replay equivalence property (the ts-dts counterpart of the jvm-abi
shortcut) are exercised, tsc-gated, in test_ts_equivalence.py.
"""

import pytest

from brix import AtomicBuilder, Graph, IncrementalBuilder, ts
from brix.data import DataFactory, FileData, ToolData
from brix.ts import DtsFile, DtsGlob

data = DataFactory()


# --- the ts-dts lens surface ---


def test_dts_lenses_file_and_glob_atoms():
    lensed = ts.dts(data.file("a/dist/lib.d.ts"))
    assert isinstance(lensed, DtsFile) and lensed.content_key == "ts-dts"

    g = ts.dts(data.glob("a/dist/{name}.d.ts"))
    assert isinstance(g, DtsGlob) and g.content_key == "ts-dts"


def test_dts_passes_through_non_file_atoms_and_already_lensed():
    tool = ToolData("tsc")
    assert ts.dts(tool) is tool  # not a file/glob: unchanged
    already = DtsFile("x.d.ts")
    # A DtsFile is a FileData subclass, not `type is FileData`, so it is left be.
    assert ts.dts(already) is already


# --- CompileAction lowering ---


def test_compile_emits_js_and_dts_globs_and_declaration_flag():
    (action,) = ts.CompileAction(
        sources=data.glob("src/{name}.ts"),
        out_dir="dist",
    ).lower()
    assert "--declaration" in action.command_line
    assert "--outDir {out_dir}" in action.command_line
    # Recursive {name} globs under out_dir track nested emit.
    assert action.outputs["js"].pattern.template == "dist/{name}.js"
    assert action.outputs["dts"].pattern.template == "dist/{name}.d.ts"
    assert action.outputs["out_dir"].path == "dist"
    assert set(action.inputs) == {"tsc", "options", "sources"}


def test_compile_without_declaration_omits_dts():
    (action,) = ts.CompileAction(
        sources=data.glob("src/{name}.ts"),
        out_dir="dist",
        declaration=False,
    ).lower()
    assert "--declaration" not in action.command_line
    assert "dts" not in action.outputs


def test_compile_options_flow_through():
    (action,) = ts.CompileAction(
        sources=data.glob("src/{name}.ts"),
        out_dir="dist",
        options=["--strict", "--target", "es2022"],
    ).lower()
    assert action.inputs["options"].text == "--strict --target es2022"


def test_dependencies_are_ts_dts_lensed_fingerprint_inputs():
    (action,) = ts.CompileAction(
        sources=data.glob("b/src/{name}.ts"),
        out_dir="b/dist",
        dependencies=[data.file("a/dist/lib.d.ts")],
    ).lower()
    types = action.inputs["types"]
    assert {a.content_key for a in types.atoms()} == {"ts-dts"}
    assert [a.path for a in types.atoms()] == ["a/dist/lib.d.ts"]
    # Fingerprint-only: tsc resolves them on disk, so no command placeholder.
    assert "{types}" not in action.command_line


def test_custom_tsc_tool_is_used():
    tool = ToolData("/proj/node_modules/.bin/tsc")
    (action,) = ts.CompileAction(
        sources=data.glob("src/{name}.ts"), out_dir="dist", tsc=tool
    ).lower()
    assert action.inputs["tsc"] is tool


# --- ts-dts is a built-in: a ts graph crosses no custom identity ---


def test_ts_graph_uses_only_the_builtin_lens():
    from brix.lowering import custom_identity_names

    g = Graph()
    g.add(
        ts.CompileAction(
            sources=g.data.glob("b/src/{name}.ts"),
            out_dir="b/dist",
            dependencies=[FileData("a/dist/lib.d.ts")],
        )
    )
    # ts-dts is core-native, so nothing crosses the engine boundary as a
    # custom content_identity callback.
    assert custom_identity_names(g) == set()


# --- P0 uniformity guard: atomic refuses a tool-self-resolved intra-build dep ---


def _cross_module_graph(root):
    g = Graph()
    d = g.data
    tsc = ToolData("tsc")
    g.add(
        ts.CompileAction(sources=d.glob("a/{n}.ts"), out_dir="a/dist", tsc=tsc),
        ts.CompileAction(
            sources=d.glob("b/{n}.ts"),
            out_dir="b/dist",
            tsc=tsc,
            dependencies=[d.file("a/dist/a.d.ts")],  # tsc self-resolves this import
        ),
    )
    return g


def test_dependencies_mark_the_action_self_resolved():
    g = _cross_module_graph(None)
    from brix.atomic import staging_hazards

    # The dependent (b) is flagged; the leaf (a) is not.
    assert staging_hazards(g) == ["tsc -> b/dist"]


def test_atomic_builder_refuses_cross_module_ts(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a/a.ts").write_text("export const x = 1;\n")
    (tmp_path / "b/b.ts").write_text("export const y = 2;\n")
    inner = IncrementalBuilder(lambda: _cross_module_graph(tmp_path), root=tmp_path)
    builder = AtomicBuilder(inner, staging_dir="build/staging")
    with pytest.raises(ValueError, match="atomic staging can't build"):
        builder.build(reporter=None)


def test_single_module_ts_is_fine_under_atomic():
    # No cross-module dependency -> no self-resolution -> no hazard.
    g = Graph()
    g.add(ts.CompileAction(sources=g.data.glob("src/{n}.ts"), out_dir="dist"))
    from brix.atomic import staging_hazards

    assert staging_hazards(g) == []
