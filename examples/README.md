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
| `typescript/` | two-module TypeScript with `brix.ts`: `ts.CompileAction` per module + the **ts-dts** lens — an impl-only edit to the greeter replays the app; an exported-signature change rebuilds it (needs a local `tsc`: `npm install typescript@5`) |
| `incremental_demo/` | build, rebuild, change a file, delete a source — watch what re-runs |
| `layers_demo/` | the middleware layer stack (incremental / cache / atomic) on a live build |
| `cli_demo/` | the CLI section of a project `brix.py` (`build`/`status`/`clean`/…) |
| `cache_store_demo/` | a pluggable two-tier team cache across machines |

Run any of them directly, e.g. `python examples/hello_text/brix.py build`.

## Real-project dogfoods (clone required)

Each has its own `README.md` with clone + setup steps. Checkouts go in
`~/Projects/brix_test/`; run with `python examples/<name>/brix.py build`.

| example | project | proves |
|---|---|---|
| `junit5/` | JUnit 5 (Gradle) | multi-module Java compile + `jvm-abi` ABI avoidance + warm javac worker; `benchmark.py` beats Gradle on the edit-compile loop. Found the jvm-abi String-annotation bug. |
| `fmt/` | fmtlib/fmt (CMake) | `brix.cpp` compile/link + automatic `#include` header-dep tracking on real headers |
| `jackson/` | jackson-core (Maven) | selective JUnit 5 test running (`java.JUnitTestAction` — two lenses), plus a generated source and a resource-bearing jar |
| `leveldb/` | google/leveldb (CMake) | C++ static library at scale: 38 TUs compiled in parallel, `cpp.ArchiveAction` → `libleveldb.a`, linked + run against a smoke program; header-dep tracking stays selective on the real tree |
| `spring-petclinic/` | spring-petclinic (Maven) | a real Spring Boot app: `java.pom("pom.xml")` drives Maven for the ~90-jar classpath, compiled + packaged + `brix.springboot.SpringBootJar` → an executable fat jar that boots with `java -jar`. brix drives the resolver and owns compile + package + incremental |
| `the_silver_searcher/` | ggreer/the_silver_searcher (autotools) | a real C tool (`ag`): `cpp.pkg_config(["libpcre","zlib","liblzma"])` resolves its system-library deps; brix compiles the 12 TUs (win32 one dropped via glob `-`) and links the working binary |

**Next dogfood targets** (see the repo README's "Feature-completeness
dogfooding" section): an annotation-processor project (add `-processorpath`
to `JarAction`), and `jackson-databind` (multi-module test selection).
