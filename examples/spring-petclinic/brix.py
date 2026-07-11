#!/usr/bin/env python3
"""Build spring-petclinic (a real Spring Boot app) with brix.

A dogfood that drives a genuine Spring Boot compile graph — 30 sources against
a ~90-jar Maven-resolved classpath (JPA, Thymeleaf, WebMVC, actuator) — through
brix's warm javac worker, packages classes + resources into a jar, and then
repackages that jar into a Spring Boot *executable* fat jar (via
`brix.springboot`) that boots with bare `java -jar`.

brix has no dependency resolver by design: it consumes Maven's resolution
(run `resolve-cp.sh` once) and owns compilation, packaging, and incremental
rebuild. See this directory's README.md.

Setup (once; first run needs network to populate ~/.m2):
    git clone --depth 1 https://github.com/spring-projects/spring-petclinic.git \
        ~/Projects/brix_test/spring-petclinic
    examples/spring-petclinic/resolve-cp.sh   # -> brix-cp-*.txt in the checkout

Run (from anywhere):
    python examples/spring-petclinic/brix.py build        # jar + executable jar
    python examples/spring-petclinic/brix.py build boot    # just the boot jar
    python examples/spring-petclinic/brix.py status
    java -jar ~/Projects/brix_test/spring-petclinic/build/petclinic-boot.jar

The checkout defaults to ~/Projects/brix_test/spring-petclinic (override with
BRIX_PETCLINIC_ROOT). This file uses the brix in this repo, not an installed
one — it puts ../../src on sys.path.
"""

import os
import sys
from pathlib import Path

# This file is named brix.py, so its own directory would shadow the brix
# package on import — drop it, then point at this repo's src/.
HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, java, springboot  # noqa: E402

ROOT = Path(
    os.environ.get(
        "BRIX_PETCLINIC_ROOT",
        os.path.expanduser("~/Projects/brix_test/spring-petclinic"),
    )
).resolve()

MAIN_CLASS = "org.springframework.samples.petclinic.PetClinicApplication"


def _read_classpath(name: str) -> list[str]:
    """The absolute jar paths Maven resolved (one ':'-joined line). These point
    into ~/.m2 and are machine-specific, so resolve-cp.sh regenerates them; they
    are not committed."""
    text = (ROOT / name).read_text().strip()
    return [p for p in text.split(os.pathsep) if p.endswith(".jar")]


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    worker = java.javac_worker(graph)

    classpath = [data.file(jar) for jar in _read_classpath("brix-cp-compile.txt")]
    graph.add(
        java.JarAction(
            source_files=data.glob("src/main/java/{name}.java"),
            output_file=data.file("build/petclinic.jar"),
            classes_dir="build/classes",
            classpath=classpath,
            resource_dir="src/main/resources",
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )

    # Repackage the plain jar into a Spring Boot executable jar: the runtime
    # dependency jars nest under BOOT-INF/lib, so `java -jar` alone boots the
    # app. brix drives Spring's own loader-tools repackager (see
    # brix.springboot) and tracks every bundled jar, so a dependency bump
    # rebuilds the executable jar.
    repackager = springboot.boot_repackager(
        graph, loader_tools_classpath=_read_classpath("brix-cp-loader-tools.txt")
    )
    runtime_jars = [data.file(jar) for jar in _read_classpath("brix-cp-runtime.txt")]
    graph.add(
        springboot.SpringBootJar(
            source_jar=data.file("build/petclinic.jar"),
            output_file=data.file("build/petclinic-boot.jar"),
            main_class=MAIN_CLASS,
            runtime_classpath=runtime_jars,
            repackager=repackager,
        )
    )
    return graph


if __name__ == "__main__":
    from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder

    def builder():
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
                cache_dir="build/.brix-cache",
            ),
            staging_dir="build/.brix-staging",
        )

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            staging_dir="build/.brix-staging",
            builder_factory=builder,
            aliases={
                "petclinic": "build/petclinic.jar",
                "boot": "build/petclinic-boot.jar",
            },
        )
    )
