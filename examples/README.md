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
| `rust_ffi/` | cross-language FFI: `brix.rust` wraps **cargo** to build a Rust C-ABI `staticlib`, and `brix.cpp` links it into a C program. brix derives the *cargo → link* edge from the artifact, so editing the Rust source reruns cargo and relinks the C app while the unchanged C compile replays. Needs `cargo` + a C compiler |
| `self_host/` | **brix builds brix**: `brix.rust` compiles brix's own Rust engine (`rust/brix-py`, a cargo workspace) — the self-hosting dogfood. Needs `cargo` |
| `typescript/` | two-module TypeScript with `brix.ts`: `ts.CompileAction` per module + the **ts-dts** lens — an impl-only edit to the greeter replays the app; an exported-signature change rebuilds it. Also gets **automatic import tracking** by default (`tsc --listFiles` → a depfile), so editing a shared source a module imports outside its own glob re-stales it (needs a local `tsc`: `npm install typescript@5`) |
| `kotlin_multimodule/` | two-module Kotlin with `brix.kotlin` and the **kotlin-abi** lens: `app` depends on `core`, which exports an `inline fun` whose body (line numbers included) kotlinc copies into `app`. Edit a normal function's body → `app` replays (the cross-module skip); edit the inline body — or just add a comment above it — → `app` recompiles. Compiles through a **warm kotlinc worker** (`kotlin.kotlinc_worker`, in-process compiler kept hot across builds via the daemon). Uses the plain incremental stack (kotlinc self-resolves `-classpath`). Needs a local `kotlinc` (`BRIX_KOTLINC=…`) |
| `annotation_processing/` | a processor jar generates application source; processor implementation changes recompile the app even when its Java ABI stays unchanged |
| `incremental_demo/` | build, rebuild, change a file, delete a source — watch what re-runs |
| `layers_demo/` | the middleware layer stack (incremental / cache / atomic) on a live build |
| `cli_demo/` | the CLI section of a project `brix.py` (`build`/`status`/`clean`/…) |
| `cache_store_demo/` | a pluggable two-tier team cache across machines |
| `hierarchical/` | a monorepo split into per-package `brix.py` files (`core → api → web` plus an independent `docs`, one shared `tools/step.py`): `load_workspace` merges them to build identically to a monolith, and `load_affected` loads **only** the packages an edit touches. `verify_lazy.py` proves it: a leaf edit loads the chain and skips `docs`, an independent edit loads only `docs`, editing the shared tool loads everything (soundness), an unreferenced edit loads nothing — each checked byte-identical against a full build |
| `polyglot_monorepo/` | the same lazy-loading, but **across languages** — a Java backend (`libs/model`, `services/orders`) and a TypeScript frontend (`shared`, `app`) in one workspace. `verify_polyglot.py` proves the cross-language partition: a backend edit loads no frontend package and vice versa, while the TS `shared → app` edge (ts-dts) still pulls in the dependent. The closest miniature of the air-gapped Java+TS target; selection needs no tools, and it builds the Java slice for real (TS where `tsc` is present) |

Run any of them directly, e.g. `python examples/hello_text/brix.py build`.

## Starter templates (copy these to begin)

Unlike the teaching examples (which *show* a feature), these are minimal,
generic, working projects meant to be **copied out** as the start of your own —
a `brix.py` wired with the recommended stack, a `build`/`run`/`status` CLI, and
one placeholder source. Copy the directory, drop in your sources, adjust a
couple of names in the `brix.py`, and build.

| template | start here for |
|---|---|
| `templates/java-service/` | a Java service → runnable jar (`java.JarAction`); needs a JDK |
| `templates/typescript-service/` | a TypeScript service → `dist/*.js` (`ts.CompileAction`); needs `tsc` |

Each `brix.py` opens with a marked block to **delete in your copy** (it only
lets the template run from inside this repo); your own project imports an
installed `brix`.

There are also two **benchmarks** (not build files): `scaling_benchmark.py`
characterizes the engine's per-build overhead as graph size grows, and
`lazy_loading_benchmark.py` quantifies the hierarchical lazy-loading win —
graph-construction time, full-load vs affected-only, across a ladder of package
counts.

And an **end-to-end dogfood**, `monorepo_dogfood.py` — a git-backed hierarchical
C monorepo (`util` static library + `app` that links it) driven by the trial's
exact workflow: git-based `load_affected` selects what to load, and a
git-accelerated incremental build (native on the Rust engine) rebuilds only
what changed. It runs an edit sequence and checks *what actually rebuilt* —
proving hierarchical loading, derived cross-package deps, lazy loading, git
detection, and C header tracking all compose. Needs `cc`/`ar`/`git`.

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
