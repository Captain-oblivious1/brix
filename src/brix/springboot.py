"""Spring Boot executable-jar packaging.

Builds on `brix.java`: it takes a plain application jar (the payload a
`java.JarAction` already produces) plus the app's Maven-resolved runtime
dependency jars, and repackages them into one executable jar via Spring's own
``spring-boot-loader-tools`` ``Repackager`` — the same code the Maven and
Gradle plugins call, so the result is byte-for-byte a standard Spring Boot jar
runnable with ``java -jar``.

brix does not reimplement the BOOT-INF layout (nested ``BOOT-INF/lib/*.jar``,
the loader classes, the ``JarLauncher`` manifest); it drives Spring's
repackager over a resolver-provided classpath. It does own the *tracking*: the
plain jar and every bundled dependency are byte-lens inputs, so the executable
jar re-stales whenever the app or any dependency it ships changes.

    repackager = springboot.boot_repackager(
        graph, loader_tools_classpath=[...]  # from Maven, see the example
    )
    graph.add(java.JarAction(..., output_file=data.file("build/app.jar")))
    graph.add(
        springboot.SpringBootJar(
            source_jar=data.file("build/app.jar"),
            output_file=data.file("build/app-boot.jar"),
            main_class="com.example.App",
            runtime_classpath=[data.file(j) for j in runtime_jars],
            repackager=repackager,
        )
    )

brix infers the order from data overlap: the boot jar's ``source_jar`` is the
JarAction's output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .actions import Action, ExternalProcessAction
from .data import DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData
from .java import _runtime_union

if TYPE_CHECKING:
    from .graph import Graph

_REPACKAGER_SOURCE = Path(__file__).parent / "_java" / "BootRepackager.java"


@dataclass(frozen=True)
class RepackagerSpec:
    """Handle to the bootstrapped ``BootRepackager`` helper: its jar plus the
    ``spring-boot-loader-tools`` runtime classpath it loads reflectively.
    Returned by `boot_repackager`; passed to `SpringBootJar`."""

    helper_jar: str
    loader_tools_classpath: tuple[str, ...]


def boot_repackager(
    graph: "Graph",
    *,
    loader_tools_classpath: list[str],
    javac: ToolData | None = None,
    jar_tool: ToolData | None = None,
    work_dir: str = "build/.brix-springboot",
) -> RepackagerSpec:
    """Add the ``BootRepackager`` bootstrap (compile + jar) to ``graph`` and
    return its spec.

    The helper is dependency-free at compile time — it loads loader-tools
    reflectively through a child class loader at run time — so this is two
    plain nodes and the helper jar is itself a fingerprinted, cacheable build
    product (the same shape as ``java.javac_worker``'s bootstrap). Idempotent:
    called more than once on a graph it adds the bootstrap only once.

    ``loader_tools_classpath`` is the ``spring-boot-loader-tools`` runtime
    classpath (loader-tools plus its transitive jars) as resolved by Maven —
    brix consumes that resolution rather than reimplementing it.
    """
    cached = getattr(graph, "_brix_boot_repackager", None)
    if cached is not None:
        return cached

    javac = javac if javac is not None else ToolData("javac")
    jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
    classes_dir = f"{work_dir}/repackager-classes"
    helper_jar = f"{work_dir}/boot-repackager.jar"
    classes = GlobData(f"{classes_dir}/{{name}}.class")

    graph.add(
        ExternalProcessAction(
            name="boot-repackager: compile",
            command_line="{javac} -d {classes_dir} {source}",
            inputs={
                "javac": javac,
                "source": FileData(str(_REPACKAGER_SOURCE.resolve())),
            },
            outputs={"classes_dir": FileData(classes_dir), "classes": classes},
        ),
        ExternalProcessAction(
            name="boot-repackager: jar",
            command_line="{jar} cfe {out} BootRepackager -C {classes_dir} .",
            inputs={
                "jar": jar_tool,
                "classes_dir": FileData(classes_dir),
                "classes": GlobData(f"{classes_dir}/{{name}}.class"),
            },
            outputs={"out": FileData(helper_jar)},
        ),
    )
    spec = RepackagerSpec(
        helper_jar=helper_jar,
        loader_tools_classpath=tuple(loader_tools_classpath),
    )
    graph._brix_boot_repackager = spec  # type: ignore[attr-defined]
    return spec


class SpringBootJar(Action):
    """Repackage a plain application jar into a Spring Boot executable jar.

    Lowers to a single process node that runs the ``BootRepackager`` helper
    (``java -jar``), which drives Spring's ``Repackager`` over the runtime
    classpath. Every jar path is passed as a data-substituted argument (never a
    baked ``-classpath``), so the command template carries no machine-specific
    path and its fingerprint stays portable across machines and caches.
    """

    def __init__(
        self,
        *,
        source_jar: FileData,
        output_file: FileData,
        main_class: str,
        runtime_classpath: "DepSet | list[DataExpr]",
        repackager: RepackagerSpec,
        java: ToolData | None = None,
    ) -> None:
        from .java import _as_classpath_list

        self.source_jar = source_jar
        self.output_file = output_file
        self.main_class = main_class
        self.runtime_classpath = _as_classpath_list(runtime_classpath) or []
        self.repackager = repackager
        self.java = java if java is not None else ToolData("java")

    def lower(self) -> list[Action]:
        # loader-tools jars and the helper jar are byte-lens inputs (a version
        # bump re-stales); the app jar and every bundled dependency are
        # byte-lens too, since their exact bytes land in the executable jar —
        # a dependency bump must rebuild it.
        tools = DataUnion(
            *[FileData(j) for j in self.repackager.loader_tools_classpath]
        )
        libs = _runtime_union(self.runtime_classpath)
        return [
            ExternalProcessAction(
                name=f"boot jar {self.output_file.path}",
                command_line=(
                    "{java} -jar {helper}"
                    " --source {source} --dest {dest} --main {main}"
                    " --tools {tools} --libs {libs}"
                ),
                inputs={
                    "java": self.java,
                    "helper": FileData(self.repackager.helper_jar),
                    "source": self.source_jar,
                    "main": ValueData(self.main_class),
                    "tools": tools,
                    "libs": libs,
                },
                outputs={"dest": self.output_file},
            )
        ]
