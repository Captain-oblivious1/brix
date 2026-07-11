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
- `java.gradle(path)` — resolve a `build.gradle`'s classpath (planned)
- `cpp.conanfile(path)` / `cpp.vcpkg_json(path)` (planned)

**Resolve inline coordinates** (declare in `brix.py`). Named by the engine. This
is the *greenfield / single-file* path.

- `java.maven([coords])` — resolve `"group:artifact:version"` via Maven (implemented)
- `java.coursier([coords])` (planned)
- `cpp.conan([refs])` / `cpp.vcpkg([ports])` / `cpp.pkg_config([names])` (planned)

Naming the engine on the coordinate case is deliberate, not cosmetic: **Maven and
Gradle apply different transitive-version-conflict rules** (Maven "nearest wins",
Gradle "highest wins"), so `java.maven([...])` and a Gradle-based resolve of the
same list can hand back *different* versions. Exposing the engine lets you match
whatever your production build ships.

## Resolution timing and caching (JVM, today)

`java.pom` / `java.maven` resolve **eagerly** at graph construction and **memoize**
the classpath under `cache_dir` (default `.brix-deps`), keyed by the pom bytes /
the coordinate list + scope. So a no-op build re-reads the cache instead of
re-running Maven; editing the pom or the coordinate list re-resolves. The actual
`mvn` invocation is isolated behind `java._maven_build_classpath` (so tests run
without a live Maven), and `mvn=`/`cwd=` let you point at a `./mvnw` wrapper.

The resolved paths are machine-specific (`~/.m2/...`), so the cache is a local
build artifact, never committed — the dependency *declarations* stay in the pom.

**Planned maturation:** make resolution itself a graph *node* (its output the
classpath), so a pom/`build.gradle` edit re-triggers resolution and downstream
recompiles through the normal incremental graph, rather than the current
eager-with-memo pre-step. And **downloading** stays out of scope: brix consumes a
resolver's output; it does not become a package manager (see the README's
"Blocked / not yet decided").

## Build-from-source dependencies

For a vendored library or a git submodule there is no external resolver and no
opaque artifact — the dependency is just more source, so you **build it with
brix**, as nodes in your own graph (the `examples/leveldb` case: a third-party
C++ library's 38 translation units compiled into `libleveldb.a`). This is where
brix is *stronger* than adopting a prebuilt jar: an edit to the vendored
dependency incrementally rebuilds only what it affects.
