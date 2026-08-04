"""Gradle dumper (onboarder frontend) — schema contract + a live end-to-end run.

Two layers:

* ``test_fixture_*`` validate a committed IR fixture against the onboarding-IR
  schema. These need no Gradle and always run — they pin the contract the
  emitter will consume, and the shape every future dumper (Maven/CMake/Make)
  must also produce.
* ``test_live_*`` run the real dumper against a Gradle project on disk. They are
  skipped unless such a project (with a wrapper) and a JDK are available, so CI
  without Gradle stays green while the capability is still provable on demand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brix.onboard.emit import emit_build_file
from brix.onboard.gradle import run_dumper

FIXTURE = Path(__file__).parent / "fixtures" / "petclinic-kotlin.ir.json"

# A real multi-module Gradle build for the live test, if present on this machine.
_LIVE_PROJECT = Path.home() / "Projects" / "brix_test" / "junit5"
_JDK_DIR = Path.home() / "Projects" / "brix_test" / "jdk"


# -- IR schema -----------------------------------------------------------------


def _validate_ir(ir: dict) -> None:
    """Assert the document satisfies the onboarding-IR contract."""
    assert ir["ir_version"] == 1
    assert ir["source_tool"] == "gradle"
    assert isinstance(ir["gradle_version"], str) and ir["gradle_version"]
    assert isinstance(ir["root"], str)
    assert isinstance(ir["modules"], list) and ir["modules"]
    assert isinstance(ir["nodes"], list) and ir["nodes"]

    for m in ir["modules"]:
        for key in ("path", "name", "project_dir", "source_sets", "dependencies", "project_deps"):
            assert key in m, f"module missing {key}"
        assert isinstance(m["source_sets"], list)
        for ss in m["source_sets"]:
            assert {"name", "java_dirs", "kotlin_dirs", "resource_dirs", "output_dirs"} <= ss.keys()
        assert isinstance(m["dependencies"], dict)
        for files in m["dependencies"].values():
            assert isinstance(files, list)
        assert isinstance(m["project_deps"], list)

    module_paths = {m["path"] for m in ir["modules"]}
    for n in ir["nodes"]:
        for key in (
            "id", "module", "task_name", "type",
            "inputs", "outputs", "depends_on", "recognized", "fidelity", "bridge",
        ):
            assert key in n, f"node missing {key}"
        assert n["module"] in module_paths, f"node {n['id']} in unknown module"
        assert isinstance(n["recognized"], bool)
        assert n["fidelity"] in ("authoritative", "verify")
        assert n["bridge"] == {"tool": "gradle", "target": n["id"]}
        # A recognized-or-declares-output node must be authoritative; anything
        # without declared outputs and unrecognized must be flagged for verify.
        if n["recognized"] or n["outputs"]:
            assert n["fidelity"] == "authoritative"
        else:
            assert n["fidelity"] == "verify"


# -- fixture (always run) ------------------------------------------------------


def test_fixture_matches_schema():
    _validate_ir(json.loads(FIXTURE.read_text()))


def test_fixture_has_resolved_classpath_and_bridges():
    ir = json.loads(FIXTURE.read_text())
    m = ir["modules"][0]
    # A resolved compile classpath is the whole point of asking Gradle.
    assert len(m["dependencies"]["compileClasspath"]) > 50
    # Spring's BootJar is an unrecognized type but declares an output, so it is
    # a trusted bridge — the design's headline case.
    bootjar = next(n for n in ir["nodes"] if n["task_name"] == "bootJar")
    assert bootjar["recognized"] is False
    assert bootjar["fidelity"] == "authoritative"
    assert bootjar["outputs"]


# -- live end-to-end (skipped without a project + JDK) -------------------------


def _java_installations():
    if not _JDK_DIR.is_dir():
        return []
    return [str(p) for p in sorted(_JDK_DIR.glob("jdk-*")) if p.is_dir()]


@pytest.mark.skipif(
    not (_LIVE_PROJECT / "gradlew").exists() or not _java_installations(),
    reason="needs a local Gradle project with a wrapper and a JDK install",
)
def test_live_multimodule_dump(tmp_path):
    out = tmp_path / "ir.json"
    ir = run_dumper(
        _LIVE_PROJECT,
        out_path=out,
        java_installations=_java_installations(),
    )
    _validate_ir(ir)
    # A real multi-module build: many modules, real cross-module edges, and the
    # bespoke code-generation tasks captured as bridges (not dropped).
    assert len(ir["modules"]) > 5
    assert sum(len(m["project_deps"]) for m in ir["modules"]) > 20
    gen = [n for n in ir["nodes"] if "generate" in n["task_name"].lower()]
    assert gen, "expected code-generation tasks to be captured"
    assert all(n["bridge"]["tool"] == "gradle" for n in gen)


# -- emitter: IR -> brix_build.py ---------------------------------------------


def test_emit_from_fixture_maps_native_spine():
    ir = json.loads(FIXTURE.read_text())
    result = emit_build_file(ir)
    # Valid Python.
    compile(result.source, "<generated>", "exec")
    src = result.source
    # The native spine: classpath delegated to Gradle, a Kotlin compile+jar with
    # the *kotlin* source glob (the dumper fix), resources, and a jar alias.
    assert "java.gradle(" in src
    assert "kotlin.JarAction(" in src
    assert "data.glob('src/main/kotlin/{name}.kt')" in src
    assert "resource_dir='src/main/resources'" in src
    assert "build/brix/spring-petclinic-kotlin.jar" in src
    assert result.emitted_modules == [":"]
    # The two undecidable facts are TODOs, never guessed values.
    assert "entry_point=" not in src.replace("# entry_point", "")  # only in a comment
    assert "spring_allopen=" not in src.replace("# spring_allopen", "")


def test_emit_notes_flag_semantic_gaps():
    ir = json.loads(FIXTURE.read_text())
    result = emit_build_file(ir)
    joined = " | ".join(result.notes)
    assert "main class" in joined
    assert "spring" in joined.lower() or "compiler plugin" in joined.lower()
    # Non-spine tasks are bridged, not dropped.
    assert any("bridged back to gradle" in n.lower() for n in result.notes)


def test_emit_bridges_delegate_non_spine_to_gradle():
    ir = json.loads(FIXTURE.read_text())
    result = emit_build_file(ir)
    compile(result.source, "<generated>", "exec")
    src = result.source
    assert result.bridged_nodes  # something real got bridged
    assert "ExternalProcessAction" in src
    assert "from brix import Graph, cli, java, kotlin, ExternalProcessAction" in src
    # A bridge re-invokes the source tool for exactly its target, with the gradle
    # tool + module inputs tracked and its outputs captured/cached.
    assert "{gradle} --console=plain -q :" in src
    assert '"gradle": data.tool(GRADLE)' in src
    # Environment/IDE tasks are blocklisted — never run as build steps.
    assert ":wrapper" not in result.bridged_nodes
    assert ":updateDaemonJvm" not in result.bridged_nodes


def test_emit_bridge_captures_directory_output_as_glob():
    # A generator that outputs a directory must be captured as FileData + a
    # recursive glob (the JavaCompile idiom), else cache restore misses files.
    ir = json.loads(FIXTURE.read_text())
    src = emit_build_file(ir).source
    # petclinic's :buildKotlinToolingMetadata writes a directory.
    assert "data.glob('build/kotlinToolingMetadata/{name}')" in src


@pytest.mark.skipif(
    not (_LIVE_PROJECT / "gradlew").exists() or not _java_installations(),
    reason="needs a local Gradle project with a wrapper and a JDK install",
)
def test_live_emit_multimodule_compiles(tmp_path):
    ir = run_dumper(_LIVE_PROJECT, out_path=tmp_path / "ir.json", java_installations=_java_installations())
    result = emit_build_file(ir)
    compile(result.source, "<generated>", "exec")
    # A real multi-module build yields several native-spine modules and bridges
    # its bespoke generators back to gradle (junit5 has many generate* tasks).
    assert len(result.emitted_modules) > 3
    assert result.bridged_nodes
    assert any("generate" in nid.lower() for nid in result.bridged_nodes)
