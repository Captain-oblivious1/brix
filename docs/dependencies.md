# Third-party dependencies

brix does **not** resolve or download third-party dependencies. Transitive
graphs, version-conflict arbitration, repository protocols, lockfiles — that is a
large, solved problem the ecosystem's tools (Maven, Gradle, Coursier, Conan,
vcpkg…) already do well, and it is orthogonal to what brix owns: the build graph
and never-miss-an-edge incremental correctness. So brix **drives a resolver and
consumes its output** as tracked file inputs.

## The shape a dependency has at build time

Strip away the ecosystem and a dependency gives a build two things: artifacts to
**find APIs against** (a JVM classpath; a C++ `-I` include dir) and artifacts to
**link/bundle** (a jar; a `.a`/`.so`), plus a few flags. brix models that as a
`DepSet` — an ordered set of resolved artifact paths plus flags
(`brix/deps.py`). The same shape serves every language; a JVM classpath is a
`DepSet` whose flags are empty.

A `DepSet` is **lens-neutral**: it carries the artifact files, and the *consumer*
applies the content lens — a compile classpath is ABI-lensed (`jvm-abi`), a
runtime classpath byte-lensed — so composing dependencies never fights
`content_key` (see `docs/content-key.md`).

It composes with brix's data algebra:

```python
deps = java.pom("pom.xml") + java.maven(["com.example:extra:1.0"])
deps = deps - "*/commons-logging-*.jar"      # drop a transitively-pulled jar
```

`+` is **ordered** merge (first occurrence wins) — load-bearing for native link
order (`-lfoo -lbar` ≠ the reverse), harmless for a JVM classpath. `-` excludes
by fnmatch (full path or basename).

## The adapter surface

Each adapter is the same thin thing — *run the ecosystem's resolver as a step,
capture its output into a `DepSet`* — and they split on one axis, not on
language:

**Adopt an existing manifest** (no coordinates; point at the project). Named by
the manifest. This is the *migration* path — keep your declarations where they
are.

- `java.pom(path)` — resolve a `pom.xml`'s declared deps (implemented)
- `java.gradle(path)` — resolve a Gradle project's configuration classpath (implemented)
- `cpp.conanfile(path)` / `cpp.vcpkg_json(path)` (planned)

**Resolve inline coordinates** (declare in `brix.py`). Named by the engine. This
is the *greenfield / single-file* path.

- `java.maven([coords])` — resolve `"group:artifact:version"` via Maven (implemented)
- `cpp.pkg_config([names])` — query installed system libraries (implemented)
- `java.coursier([coords])` (planned)
- `cpp.conan([refs])` / `cpp.vcpkg([ports])` (planned)

`cpp.pkg_config` returns a `DepSet` whose `--cflags` are its `compile_flags`
(`-I`/`-D`) and `--libs` its `link_flags` (`-L`/`-l`).
`CompileSourceAction(deps=...)` adds the compile flags to every translation
unit; `LinkApplicationAction(deps=...)` adds the link flags *after* the objects
(link order) plus any tracked artifact files. System libraries come as `-l`
flags, not file paths, so they are not fingerprinted — a system-lib upgrade is
outside the project (a Conan/vcpkg adapter yielding concrete `.a`/`.so` paths
would populate `artifacts`, which the link step does track).

Naming the engine on the coordinate case is deliberate, not cosmetic: **Maven and
Gradle apply different transitive-version-conflict rules** (Maven "nearest wins",
Gradle "highest wins"), so `java.maven([...])` and a Gradle-based resolve of the
same list can hand back *different* versions. Exposing the engine lets you match
whatever your production build ships.

## Resolution timing and caching (JVM, today)

`java.pom` / `java.maven` / `java.gradle` resolve **eagerly** at graph
construction and **memoize** the classpath under `cache_dir` (default
`.brix-deps`). So a no-op build re-reads the cache instead of re-running the
resolver; editing what the key covers re-resolves. The resolver invocation is
isolated behind a seam (`java._maven_build_classpath`,
`java._gradle_build_classpath`) so tests run without a live Maven/Gradle, and
`mvn=`/`gradle=`/`cwd=` let you point at a `./mvnw` or `./gradlew` wrapper.

The cache key differs by adapter because the manifest does:

- **Maven** (`pom`/`maven`): the pom bytes (or coordinate list) + scope. A pom is
  largely self-contained.
- **Gradle** (`gradle`): the bytes of *all* dependency-declaring files in the
  project dir — `build.gradle(.kts)`, `settings.gradle(.kts)`,
  `gradle.properties`, and a `gradle/libs.versions.toml` version catalog — plus
  the configuration name and target project path. Gradle spreads declarations
  across files, so keying on `build.gradle` alone would miss a catalog bump.
  Limitation: only the target project's own directory is scanned, so a version
  catalog at the *repo root* of a multi-project build is not seen by the key —
  bump a build file, or clear `.brix-deps`, to force re-resolution in that case.

Gradle has no `dependency:build-classpath` goal, so `java.gradle` injects a
temporary **init script** (`--init-script`, never editing the project's own
build files) that writes a configuration's resolved `files`. Pass `project=` (a
Gradle project path like `":service-a"`) to target a subproject of a
multi-project build, and `configuration=` to pick `compileClasspath` (default) /
`runtimeClasspath` / `testRuntimeClasspath` — the Gradle analog of a Maven scope.
Config and output path are baked into the generated script, not passed as `-P`
properties: some builds validate project properties and reject unknown ones
(junit5's `build-parameters` plugin does), so external properties are a
collision surface `java.gradle` avoids.

`java.gradle` returns only **external** artifacts (jars): a sibling
project-dependency in a multi-project build resolves to a class *directory*,
which is dropped. That is the right split for the monorepo case — those sibling
modules are brix graph nodes you build, not opaque jars you adopt.

The resolved paths are machine-specific (`~/.m2/...`, `~/.gradle/caches/...`), so
the cache is a local build artifact, never committed — the dependency
*declarations* stay in the pom / build files.

**Planned maturation:** make resolution itself a graph *node* (its output the
classpath), so a pom/`build.gradle` edit re-triggers resolution and downstream
recompiles through the normal incremental graph, rather than the current
eager-with-memo pre-step. And **downloading** stays out of scope: brix consumes a
resolver's output; it does not become a package manager (see the roadmap's
[blocked and undecided section](roadmap.md#blocked-or-not-yet-decided)).

## Build-from-source dependencies

For a vendored library or a git submodule there is no external resolver and no
opaque artifact — the dependency is just more source, so you **build it with
brix**, as nodes in your own graph (the `examples/leveldb` case: a third-party
C++ library's 38 translation units compiled into `libleveldb.a`). This is where
brix is *stronger* than adopting a prebuilt jar: an edit to the vendored
dependency incrementally rebuilds only what it affects.
