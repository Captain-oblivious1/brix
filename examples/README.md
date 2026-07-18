# Examples

Two kinds: **teaching examples** are self-contained (their sources live here;
run them directly), and **real-project dogfoods** build famous open-source
projects you clone into `~/Projects/brix_test/` — the `brix.py` lives here,
pointed at the checkout. The dogfoods are how brix's real-world coverage gets
proven (each one has found a real bug or missing feature).

## Teaching examples (self-contained)

| example | what it shows |
|---|---|
| `hello_text/` | the basics — uppercase each `src/*.txt`, then combine them |
| `raw_cpp/` | the raw C++ graph from the design doc: compile each `.cpp`, link an app |
| `cpp_app/` | a lifelike C++ project: typed `brix.cpp` actions, the full layer stack + CLI |
| `cpp_to_java/` | composability: C++, generic, and Java actions in one graph |
| `typescript/` | two-module TypeScript with `brix.ts`: `ts.CompileAction` per module + the **ts-dts** lens — an impl-only edit to the greeter replays the app; an exported-signature change rebuilds it. Also gets **automatic import tracking** by default (`tsc --listFiles` → a depfile), so editing a shared source a module imports outside its own glob re-stales it (needs a local `tsc`: `npm install typescript@5`) |
| `kotlin_multimodule/` | two-module Kotlin with `brix.kotlin` and the **kotlin-abi** lens: `app` depends on `core`, which exports an `inline fun` whose body (line numbers included) kotlinc copies into `app`. Edit a normal function's body → `app` replays (the cross-module skip); edit the inline body — or just add a comment above it — → `app` recompiles. Compiles through a **warm kotlinc worker** (`kotlin.kotlinc_worker`, in-process compiler kept hot across builds via the daemon). Uses the plain incremental stack (kotlinc self-resolves `-classpath`). Needs a local `kotlinc` (`BRIX_KOTLINC=…`) |
| `annotation_processing/` | a processor jar generates application source; processor implementation changes recompile the app even when its Java ABI stays unchanged |
| `incremental_demo/` | build, rebuild, change a file, delete a source — watch what re-runs |
| `layers_demo/` | the middleware layer stack (incremental / cache / atomic) on a live build |
| `cli_demo/` | the CLI section of a project `brix.py` (`build`/`status`/`clean`/…) |
| `cache_store_demo/` | a pluggable two-tier team cache across machines |
| `hierarchical/` | a monorepo split into per-package `brix.py` files (`core → api → web` plus an independent `docs`, one shared `tools/step.py`): `load_workspace` merges them to build identically to a monolith, and `load_affected` loads **only** the packages an edit touches. `verify_lazy.py` proves it: a leaf edit loads the chain and skips `docs`, an independent edit loads only `docs`, editing the shared tool loads everything (soundness), an unreferenced edit loads nothing — each checked byte-identical against a full build |

Run any of them directly, e.g. `python examples/hello_text/brix.py build`.

There are also two **benchmarks** (not build files): `scaling_benchmark.py`
characterizes the engine's per-build overhead as graph size grows, and
`lazy_loading_benchmark.py` quantifies the hierarchical lazy-loading win —
graph-construction time, full-load vs affected-only, across a ladder of package
counts.

## Real-project dogfoods (clone required)

Each has its own `README.md` with clone + setup steps. Checkouts go in
`~/Projects/brix_test/`; run with `python examples/<name>/brix.py build`.

| example | project | proves |
|---|---|---|
| `junit5/` | JUnit 5 (Gradle) | multi-module Java compile + `jvm-abi` ABI avoidance + warm javac worker; `benchmark.py` beats Gradle on the edit-compile loop. Found the jvm-abi String-annotation bug. |
| `fmt/` | fmtlib/fmt (CMake) | `brix.cpp` compile/link + automatic `#include` header-dep tracking on real headers |
| `jackson/` | Jackson core + annotations + databind | three real modules and selective JUnit suites: a core implementation edit reruns core/databind tests without recompiling databind or touching annotations; includes an automated reversible verifier |
| `leveldb/` | google/leveldb (CMake) | C++ static library at scale: 38 TUs compiled in parallel, `cpp.ArchiveAction` → `libleveldb.a`, linked + run against a smoke program; header-dep tracking stays selective on the real tree |
| `spring-petclinic/` | spring-petclinic (Maven) | a real Spring Boot app: `java.pom("pom.xml")` drives Maven for the ~90-jar classpath, compiled + packaged + `brix.springboot.SpringBootJar` → an executable fat jar that boots with `java -jar`. brix drives the resolver and owns compile + package + incremental |
| `spring-petclinic-kotlin/` | spring-petclinic-kotlin (Gradle KTS) | the Kotlin Spring Boot app: `java.gradle(...)` adopts the Kotlin-DSL build's `compileClasspath`, `brix.kotlin` compiles the module with the Spring **all-open** plugin (`spring_allopen=`, the `kotlin("plugin.spring")` equivalent — a correctness feature: without it Spring-annotated classes stay `final` and fail to proxy at boot), and the app boots a full context against the resolved runtime classpath |
| `the_silver_searcher/` | ggreer/the_silver_searcher (autotools) | a real C tool (`ag`): `cpp.pkg_config(["libpcre","zlib","liblzma"])` resolves its system-library deps; brix compiles the 12 TUs (win32 one dropped via glob `-`) and links the working binary |

**Next dogfood target** (see the [roadmap's feature-completeness dogfooding
section](../docs/roadmap.md#feature-completeness-dogfooding-in-progress--prove-brix-on-real-projects-before-starting-track-3)):
`jackson-databind` (multi-module test selection).
