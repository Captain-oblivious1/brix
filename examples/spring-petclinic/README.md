# Building spring-petclinic with brix

A real-world acceptance test: build a genuine Spring Boot application — the
canonical [spring-petclinic](https://github.com/spring-projects/spring-petclinic)
(Spring Boot 4.1, JPA/Hibernate + Thymeleaf + Spring WebMVC + actuator) — with
brix, all the way to an executable jar that boots with bare `java -jar`. This is
the dogfood that drove `brix.springboot` (Spring Boot fat-jar packaging).

## What it demonstrates

- **brix drives a resolver, not reimplements one.** `java.pom("pom.xml")` adopts
  petclinic's existing Maven manifest — brix runs Maven (via the checkout's
  `./mvnw`), reads the resolved classpath (~90 compile / ~106 runtime jars), and
  owns compilation, packaging, and incremental rebuild from there. No coordinates
  are re-declared and no classpath files are committed; resolution is memoized
  under `build/.brix-deps` and re-runs only when the pom changes.
- **A real compile graph**: 30 sources against the full Spring classpath through
  the warm javac worker, with `src/main/resources` (templates, SQL, static
  assets, `application.properties`) packaged into the jar.
- **Content-addressed incrementality on real code**: a comment-only edit
  recompiles (the source changed) but does *not* re-jar — the emitted `.class`
  bytes are identical, so the jar step replays. A real code change cascades
  recompile → re-jar → re-bootjar.
- **Spring Boot executable-jar packaging** (`brix.springboot.SpringBootJar`):
  the plain jar plus the runtime dependency jars are repackaged into one
  executable fat jar (nested `BOOT-INF/lib`, `JarLauncher` manifest) via
  Spring's own `spring-boot-loader-tools` — so the output is a standard Boot jar.
  brix does not reimplement the layout; it drives Spring's repackager and tracks
  every bundled jar, so a dependency bump rebuilds the executable jar.

## Setup

    # clone the checkout (not committed; ~/Projects/brix_test keeps clones
    # out of the Projects root):
    git clone --depth 1 https://github.com/spring-projects/spring-petclinic.git \
        ~/Projects/brix_test/spring-petclinic

That's it — the first `build` resolves dependencies itself (via the checkout's
`./mvnw`; needs network the first time to populate `~/.m2`) and memoizes the
result. Override the checkout location with `BRIX_PETCLINIC_ROOT`.

## Run

    python examples/spring-petclinic/brix.py build        # jar + executable jar
    python examples/spring-petclinic/brix.py build boot    # just the boot jar
    python examples/spring-petclinic/brix.py status

    java -jar ~/Projects/brix_test/spring-petclinic/build/petclinic-boot.jar
    # then: curl localhost:8080/actuator/health  ->  {"status":"UP"}
    #       curl localhost:8080/vets             ->  seeded JSON

The build uses the brix in this repo (it puts `../../src` on `sys.path`) and the
Rust engine by default, with the warm javac worker persisting between builds.

## Notes

- **The plain jar is not runnable on its own** — that is expected. A plain jar
  has no bundled dependencies; `SpringBootJar` is what makes `java -jar` work.
- **The executable jar boots slower than a flat classpath** (nested-jar loading
  has overhead). That is inherent to the Spring Boot jar format, not brix.
- No annotation processors here (petclinic uses none). Lombok/`-processorpath`
  is the next Java dogfood — see the
  [roadmap](../../docs/roadmap.md#feature-completeness-dogfooding-in-progress--prove-brix-on-real-projects-before-starting-track-3).
