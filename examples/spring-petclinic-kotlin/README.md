# Building spring-petclinic-kotlin with brix

The Kotlin counterpart of the [`spring-petclinic/`](../spring-petclinic/)
(Maven, Java) dogfood: build a real Kotlin Spring Boot application — the
canonical [spring-petclinic-kotlin](https://github.com/spring-petclinic/spring-petclinic-kotlin)
(Spring Boot 4.1, Kotlin 2.4, JPA/Hibernate on H2 + Spring WebMVC + actuator) —
with brix. This is the dogfood that drove `brix.kotlin`.

## What it demonstrates

- **Gradle dependency adoption on a Kotlin-DSL build.** `java.gradle(...)`
  resolves the project's own `compileClasspath` through an injected init script
  (the project's build files are never edited); the `build.gradle.kts`,
  `settings.gradle.kts`, and `gradle/libs.versions.toml` are the cache key.
  brix owns compilation and incrementality — Gradle only resolves.
- **The Spring all-open compiler plugin as a *correctness* feature.** Kotlin
  classes are `final` by default, which silently defeats Spring's runtime bean
  proxying: the app compiles clean and then fails to boot. Gradle applies
  `kotlin("plugin.spring")`; `brix.kotlin` reproduces it via `spring_allopen=`
  (the all-open plugin with its `spring` preset), so this is a first-class
  argument rather than something you remember to assemble. The effect is
  visible in the bytecode:

      # with the plugin (as brix builds it):     public class CacheConfig
      # without it:                          public final class CacheConfig

  and it is what lets the app boot (see below).
- **Whole-module kotlinc semantics with content-aware skipping.** kotlinc
  compiles the module as a unit, so one edited source recompiles every class
  (correct, not a regression). brix's content-addressed jar step still replays
  when a recompile emits byte-identical classes — e.g. a comment-only edit
  recompiles but does not re-jar.

Cross-module Kotlin is skipped through the **kotlin-abi** lens, not `jvm-abi`:
a Kotlin `inline fun` copies its body into other modules' call sites, so the
"body is not the interface" assumption is unsound for a *consumed* Kotlin
module — the lens counts inline-carrying classes by bytes and everything else
by ABI. This app is single-module, so the question barely arises here; see
`examples/kotlin_multimodule/` for the lens in action and
`src/brix/kotlinabi.py` for the design.

## Setup

    # 1. Clone the checkout (kept out of the Projects root):
    git clone --depth 1 https://github.com/spring-petclinic/spring-petclinic-kotlin.git \
        ~/Projects/brix_test/spring-petclinic-kotlin

    # 2. A JDK 17 (the project pins languageVersion=17) and a kotlinc dist.
    #    Point Gradle's toolchain detection at the JDK 17, e.g. add to
    #    ~/.gradle/gradle.properties:
    #        org.gradle.java.installations.paths=/path/to/jdk-17

Environment overrides (all optional; dev defaults in `brix.py`):

    BRIX_PETCLINIC_KOTLIN_ROOT   the checkout location
    BRIX_KOTLINC                 kotlinc launcher (default: on PATH)
    BRIX_KOTLIN_ALLOPEN          allopen-compiler-plugin.jar in the kotlinc dist
    BRIX_KOTLIN_GRADLE           gradle launcher (default: the project ./gradlew)

## Run

    python examples/spring-petclinic-kotlin/brix.py build     # resolve + compile + jar
    python examples/spring-petclinic-kotlin/brix.py status

The build uses the brix in this repo (it puts `../../src` on `sys.path`) and the
Rust engine by default, through the full atomic/cache/incremental layer stack.

## Boot proof

The jar carries a `Main-Class` manifest but bundles no dependencies, so it boots
against the resolved **runtime** classpath (H2, tomcat-embed, kotlin-stdlib):

    RT=$(python - <<'PY'
    import sys; sys.path.insert(0, "src")
    from brix import java
    from pathlib import Path
    root = Path("~/Projects/brix_test/spring-petclinic-kotlin").expanduser()
    dep = java.gradle(root, gradle="$BRIX_KOTLIN_GRADLE",
                      configuration="runtimeClasspath",
                      cache_dir=str(root/"build/.brix-deps"), cwd=str(root))
    print(":".join(dep.artifacts))
    PY
    )
    JAR=~/Projects/brix_test/spring-petclinic-kotlin/build/brix/petclinic.jar
    java -cp "$JAR:$RT" \
        org.springframework.samples.petclinic.PetClinicApplicationKt --server.port=0

Expected: `Started PetClinicApplicationKt in N seconds` — a full context boots
(Tomcat, JPA/Hibernate on H2, Spring Data repositories). Booting proves the
all-open plugin did its job at run time; without it, Spring cannot proxy the
`final` beans and startup fails.

## Notes

- **A fat jar (`java -jar` with no external classpath)** is produced by
  `brix.springboot.SpringBootJar`, which is language-agnostic and already proven
  in the Java [`spring-petclinic/`](../spring-petclinic/) dogfood. It needs the
  `spring-boot-loader-tools` classpath resolved; this Gradle-only checkout has
  no Maven wrapper to resolve that with, so the example stops at the plain jar
  and boots against the runtime classpath instead.
- The jvm-abi lens on the *compile* classpath (external dependency jars) is
  sound and shared with `brix.java`; only *this module's own* outputs are kept
  off the lens, per the inline-function note above.
