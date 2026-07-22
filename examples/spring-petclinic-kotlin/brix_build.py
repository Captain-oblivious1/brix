#!/usr/bin/env python3
"""Build spring-petclinic-kotlin (a real Kotlin Spring Boot app) with brix.

The Kotlin counterpart of the `spring-petclinic/` (Maven, Java) dogfood. It
drives a genuine Kotlin Spring Boot compile — 24 Kotlin sources against a
Gradle-resolved classpath — through `brix.kotlin`, then packages the classes
and resources into an executable-manifest jar.

What this dogfood proves that the Java one does not:

- **Gradle dependency adoption on a KTS build.** `java.gradle(...)` resolves the
  project's own `compileClasspath` through an injected init script; the Kotlin
  DSL build files (`build.gradle.kts`, `settings.gradle.kts`,
  `gradle/libs.versions.toml`) are the cache key. brix owns compilation and
  incrementality; Gradle only resolves.

- **The Spring all-open compiler plugin as a correctness feature.** Kotlin
  classes are `final` by default, which silently defeats Spring's runtime bean
  proxying (it compiles clean, then fails to boot). Gradle applies
  `kotlin("plugin.spring")`; `brix.kotlin` reproduces it via `spring_allopen=`,
  which runs the all-open plugin with its `spring` preset. Omitting it is the
  trap, so it is a first-class argument. With it, `CacheConfig` compiles to a
  non-`final` class and the app boots (see README for the boot proof).

- **Whole-module kotlinc semantics.** kotlinc compiles the module as a unit, so
  one edited source recompiles every class — correct, not a regression. brix's
  content-aware jar step still skips re-jarring when a recompile produces
  byte-identical classes (e.g. a comment-only edit).

Cross-module Kotlin is deliberately *not* skipped through an ABI lens here: a
Kotlin `inline fun` copies its body into other modules' call sites, so the
`jvm-abi` "body is not interface" assumption is unsound for a consumed Kotlin
module. This app is single-module, so the question does not arise; a
soundness-checked Kotlin ABI lens is future work (docs/roadmap.md).

Setup (see README.md): clone the project into ~/Projects/brix_test/, install a
JDK 17 and kotlinc, and point Gradle's toolchain at the JDK 17. Environment
overrides (all optional, with dev defaults):

    BRIX_PETCLINIC_KOTLIN_ROOT   the checkout (default ~/Projects/brix_test/...)
    BRIX_KOTLINC                  kotlinc launcher (default: on PATH)
    BRIX_KOTLIN_ALLOPEN          allopen-compiler-plugin.jar in the kotlinc dist
    BRIX_KOTLIN_GRADLE           gradle launcher (default: the project ./gradlew)

Run (from anywhere):
    python examples/spring-petclinic-kotlin/brix_build.py build
    python examples/spring-petclinic-kotlin/brix_build.py status
"""

import os
import sys
from pathlib import Path

# Point at this repository's source tree for the in-tree example.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, java, kotlin  # noqa: E402

ROOT = Path(
    os.environ.get(
        "BRIX_PETCLINIC_KOTLIN_ROOT",
        os.path.expanduser("~/Projects/brix_test/spring-petclinic-kotlin"),
    )
).resolve()

KOTLINC = os.environ.get("BRIX_KOTLINC", "kotlinc")
ALLOPEN = os.environ.get(
    "BRIX_KOTLIN_ALLOPEN",
    os.path.expanduser("~/Projects/brix_test/kotlinc/lib/allopen-compiler-plugin.jar"),
)
GRADLE = os.environ.get("BRIX_KOTLIN_GRADLE", str(ROOT / "gradlew"))

MAIN_CLASS = "org.springframework.samples.petclinic.PetClinicApplicationKt"


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    # Adopt the project's own Gradle build — no coordinates re-declared here.
    # Resolution is memoized under build/.brix-deps and re-runs only when a
    # build script / version catalog changes.
    classpath = java.gradle(
        ROOT,
        gradle=GRADLE,
        configuration="compileClasspath",
        cache_dir=str(ROOT / "build/.brix-deps"),
        cwd=str(ROOT),
    )

    graph.add(
        kotlin.JarAction(
            source_files=data.glob("src/main/kotlin/{name}.kt"),
            output_file=data.file("build/brix/petclinic.jar"),
            kotlinc=data.tool(KOTLINC),
            classes_dir="build/brix/kotlin-classes",
            classpath=classpath,
            jvm_target="17",
            spring_allopen=ALLOPEN,  # == Gradle's kotlin("plugin.spring")
            resource_dir="src/main/resources",
            entry_point=MAIN_CLASS,
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
            aliases={"petclinic": "build/brix/petclinic.jar"},
        )
    )
