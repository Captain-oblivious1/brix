"""brix.springboot executable-jar packaging: lowering shape and bootstrap.

The end-to-end proof (a real Spring Boot app repackaged and booted with
`java -jar`) lives in the spring-petclinic dogfood, not the unit suite — these
tests pin the graph shape so the fingerprint contract can't regress silently.
"""

from brix import ExternalProcessAction, Graph, springboot
from brix.data import DataFactory
from brix.springboot import RepackagerSpec

data = DataFactory()

LOADER_TOOLS = ["lib/spring-boot-loader-tools.jar", "lib/commons-compress.jar"]


def _spec():
    return RepackagerSpec(
        helper_jar="build/.brix-springboot/boot-repackager.jar",
        loader_tools_classpath=tuple(LOADER_TOOLS),
    )


def _boot_jar(**overrides):
    kwargs = dict(
        source_jar=data.file("build/app.jar"),
        output_file=data.file("build/app-boot.jar"),
        main_class="com.example.App",
        runtime_classpath=[data.file("lib/dep-a.jar"), data.file("lib/dep-b.jar")],
        repackager=_spec(),
    )
    kwargs.update(overrides)
    return springboot.SpringBootJar(**kwargs)


def test_boot_repackager_bootstraps_compile_then_jar():
    graph = Graph()
    spec = springboot.boot_repackager(graph, loader_tools_classpath=LOADER_TOOLS)
    compile_helper, jar_helper = graph.actions
    assert isinstance(compile_helper, ExternalProcessAction)
    assert compile_helper.name == "boot-repackager: compile"
    # The helper is dependency-free at compile time (no classpath on javac).
    assert "-classpath" not in compile_helper.command_line
    assert "-cp" not in compile_helper.command_line
    # Jarred with an entry point so `java -jar helper.jar` runs BootRepackager.
    assert "cfe {out} BootRepackager" in jar_helper.command_line
    assert spec.helper_jar == "build/.brix-springboot/boot-repackager.jar"
    assert spec.loader_tools_classpath == tuple(LOADER_TOOLS)


def test_boot_repackager_is_idempotent_per_graph():
    graph = Graph()
    first = springboot.boot_repackager(graph, loader_tools_classpath=LOADER_TOOLS)
    second = springboot.boot_repackager(graph, loader_tools_classpath=LOADER_TOOLS)
    assert first is second
    # Only one bootstrap pair added, not four.
    assert len(graph.actions) == 2


def test_boot_jar_lowers_to_single_repackage_node():
    graph = Graph()
    graph.add(_boot_jar())
    (repackage,) = graph.actions
    assert isinstance(repackage, ExternalProcessAction)
    assert repackage.name == "boot jar build/app-boot.jar"
    # Launched via -jar; every jar path is a data-substituted arg, never a
    # baked -classpath, so the command template carries no machine path.
    assert "-jar {helper}" in repackage.command_line
    assert "--tools {tools}" in repackage.command_line
    assert "--libs {libs}" in repackage.command_line
    assert repackage.outputs["dest"].path == "build/app-boot.jar"


def test_boot_jar_inputs_are_byte_lens():
    """The app jar and every bundled dependency ship by value, so they are
    byte-lens inputs (no content_key) — a dependency bump rebuilds the jar."""
    graph = Graph()
    graph.add(_boot_jar())
    (repackage,) = graph.actions

    source = list(repackage.inputs["source"].atoms())
    assert [a.path for a in source] == ["build/app.jar"]
    assert all(a.content_key is None for a in source)

    libs = list(repackage.inputs["libs"].atoms())
    assert [a.path for a in libs] == ["lib/dep-a.jar", "lib/dep-b.jar"]
    assert all(a.content_key is None for a in libs)  # byte lens: impl matters

    tools = [a.path for a in repackage.inputs["tools"].atoms()]
    assert tools == LOADER_TOOLS
    assert repackage.inputs["main"].text == "com.example.App"


def test_boot_jar_main_class_threads_through():
    graph = Graph()
    graph.add(_boot_jar(main_class="org.acme.Boot"))
    (repackage,) = graph.actions
    assert repackage.inputs["main"].text == "org.acme.Boot"
