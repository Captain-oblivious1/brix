#!/usr/bin/env python3
"""Build spring-petclinic (a real Spring Boot app) with brix.

A dogfood that drives a genuine Spring Boot compile graph — 30 sources against
a ~90-jar Maven-resolved classpath (JPA, Thymeleaf, WebMVC, actuator) — through
brix's warm javac worker, packages classes + resources into a jar, and then
repackages that jar into a Spring Boot *executable* fat jar (via
`brix.springboot`) that boots with bare `java -jar`.

brix has no dependency resolver by design: it *drives* one and consumes the
result. `java.pom(...)` adopts petclinic's existing pom.xml — Maven resolves the
declared dependencies, brix reads the classpath and owns compilation, packaging,
and incremental rebuild. No separate resolve step or committed classpath files;
resolution is memoized (build/.brix-deps) and re-runs only when the pom changes.

Setup (once; first build needs network to populate ~/.m2):
    git clone --depth 1 https://github.com/spring-projects/spring-petclinic.git \
        ~/Projects/brix_test/spring-petclinic

Run (from anywhere):
    python examples/spring-petclinic/brix.py build        # resolve + jar + boot jar
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
SPRING_BOOT_VERSION = "4.1.0"  # matches the pom's spring-boot-starter-parent

# Resolve through petclinic's own Maven wrapper, in the checkout dir, memoized
# under build/.brix-deps (re-runs only when the pom / coordinates change).
_RESOLVE = dict(
    mvn=str(ROOT / "mvnw"), cwd=str(ROOT), cache_dir=str(ROOT / "build/.brix-deps")
)


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    worker = java.javac_worker(graph)

    # Adopt petclinic's existing pom.xml — no coordinates re-declared here.
    graph.add(
        java.JarAction(
            source_files=data.glob("src/main/java/{name}.java"),
            output_file=data.file("build/petclinic.jar"),
            classes_dir="build/classes",
            classpath=java.pom(ROOT / "pom.xml", scope="compile", **_RESOLVE),
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
    loader_tools = java.maven(
        [f"org.springframework.boot:spring-boot-loader-tools:{SPRING_BOOT_VERSION}"],
        scope="runtime",
        **_RESOLVE,
    )
    repackager = springboot.boot_repackager(
        graph, loader_tools_classpath=list(loader_tools.artifacts)
    )
    graph.add(
        springboot.SpringBootJar(
            source_jar=data.file("build/petclinic.jar"),
            output_file=data.file("build/petclinic-boot.jar"),
            main_class=MAIN_CLASS,
            runtime_classpath=java.pom(ROOT / "pom.xml", scope="runtime", **_RESOLVE),
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
