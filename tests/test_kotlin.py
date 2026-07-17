"""Typed Kotlin action: the `kotlin.JarAction` lowering.

Whole-module kotlinc compile + jar, the classpath join (baked value plus an
ABI-lensed fingerprint input, like JUnitTestAction's run step), and the Spring
all-open plugin wiring — the last being a correctness feature, not a
convenience: without it Spring-annotated classes stay ``final`` and fail to
proxy at run time. The real compile against spring-petclinic-kotlin is exercised
in examples/spring-petclinic-kotlin/.
"""

import os

from brix import Graph, kotlin
from brix.data import DataFactory, FileData, GlobData, ToolData

data = DataFactory()


def _lower(**overrides):
    kwargs = dict(
        source_files=data.glob("src/main/kotlin/{name}.kt"),
        output_file=data.file("build/app.jar"),
        classes_dir="build/kclasses",
    )
    kwargs.update(overrides)
    return kotlin.JarAction(**kwargs).lower()


def test_lowers_to_compile_then_jar():
    compile_k, package = _lower()
    assert compile_k.command_line.startswith("{kotlinc}")
    assert "-jvm-target {jvm_target}" in compile_k.command_line
    assert "-d {classes_dir}" in compile_k.command_line
    assert compile_k.command_line.strip().endswith("{sources}")
    assert package.command_line.startswith("{jar} cf {output}")


def test_whole_module_output_is_recursive_class_glob():
    compile_k, _ = _lower()
    classes = compile_k.outputs["classes"]
    assert isinstance(classes, GlobData)
    assert classes.pattern.template == "build/kclasses/{name}.class"


def test_classpath_is_joined_value_plus_kotlin_abi_fingerprint_input():
    compile_k, _ = _lower(
        classpath=[FileData("/d/spring.jar"), FileData("/d/kotlin-stdlib.jar")],
    )
    # One pathsep-joined -classpath token for kotlinc...
    assert "-classpath {classpath}" in compile_k.command_line
    joined = compile_k.inputs["classpath"].text
    assert joined == os.pathsep.join(["/d/spring.jar", "/d/kotlin-stdlib.jar"])
    # ...and the same jars tracked through the kotlin-abi lens, not referenced
    # by the template. Deliberately NOT jvm-abi (a Kotlin inline fun copies its
    # body into consumers, so plain ABI-lensing once skipped a recompile whose
    # output had changed) and no longer plain bytes (which recompiled dependents
    # on every implementation edit). kotlin-abi byte-counts exactly the
    # inline-carrying classes. See test_kotlin_multimodule.py for the
    # end-to-end proof and test_kotlinabi.py for the lens itself.
    track = compile_k.inputs["classpath_track"]
    assert all(getattr(a, "content_key", None) == "kotlin-abi" for a in track.atoms())
    assert "{classpath_track}" not in compile_k.command_line


def test_no_classpath_omits_both_classpath_inputs():
    compile_k, _ = _lower()
    assert "classpath" not in compile_k.inputs
    assert "classpath_track" not in compile_k.inputs
    assert "-classpath" not in compile_k.command_line


def test_spring_allopen_wires_the_plugin_and_preset():
    compile_k, _ = _lower(spring_allopen="/kotlinc/lib/allopen-compiler-plugin.jar")
    # -Xplugin=<jar> glues the byte-tracked plugin path; -P carries the preset.
    assert "-Xplugin={allopen} -P {preset}" in compile_k.command_line
    assert compile_k.inputs["allopen"].path == "/kotlinc/lib/allopen-compiler-plugin.jar"
    assert "preset=spring" in compile_k.inputs["preset"].text


def test_no_spring_allopen_leaves_classes_final_by_default():
    # The trap this argument exists to avoid: omit it and no plugin is wired,
    # so Spring-annotated classes compile final and silently fail to proxy.
    compile_k, _ = _lower()
    assert "-Xplugin" not in compile_k.command_line
    assert "allopen" not in compile_k.inputs
    assert "preset" not in compile_k.inputs


def test_entry_point_and_resources_reach_the_jar_step():
    _, package = _lower(entry_point="com.example.AppKt", resource_dir="src/main/resources")
    assert package.command_line.startswith("{jar} cfe {output} {entry_point}")
    assert "-C {resource_dir} ." in package.command_line
    assert package.inputs["resources"].pattern.template == "src/main/resources/{name}"


def test_custom_kotlinc_and_jar_tools_are_used():
    compile_k, package = _lower(
        kotlinc=ToolData("/opt/kotlinc/bin/kotlinc"),
        jar_tool=ToolData("/opt/jdk/bin/jar"),
    )
    assert compile_k.inputs["kotlinc"].name == "/opt/kotlinc/bin/kotlinc"
    assert package.inputs["jar"].name == "/opt/jdk/bin/jar"


def test_jar_repackages_on_any_class_change():
    # The jar step's class glob is the compile step's output glob, so a changed
    # class re-jars (dependency inferred from the shared collection).
    compile_k, package = _lower()
    assert package.inputs["classes"].pattern.template == "build/kclasses/{name}.class"
    assert compile_k.outputs["classes"].pattern.template == "build/kclasses/{name}.class"
