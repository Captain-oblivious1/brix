# The onboarder — design contract

Status: **shipped (Gradle → pure-Java/Kotlin JVM).** The dumper, the tool-neutral
IR, the emitter (native spine, cross-module wiring, Spring Boot fat jar,
generated-source edges, bridges), and the one-shot CLI are built and proven on real
projects. This document pins *why it is shaped the way it is* so the next increment
(a new frontend, mixed-language modules) can be added without re-deriving the design
from the code.

## 1. Why the onboarder exists

The long-term target is a ~50-service Java+TypeScript monorepo that is **air-gapped
and has no AI on its network**. So the mechanical substitute for AI-authoring a
`brix_build.py` per service is a tool that reads an *existing* build's already-resolved
model and writes the starting `brix_build.py` for you. It does not aim for "80% then
you finish by hand": it aims for a build that **works end-to-end immediately** (native
where brix understands the task, a bridge back to the source tool where it does not),
with the handful of facts the source model cannot carry surfaced as explicit `# TODO`s.

The driver's Java side is **Gradle, pure Java** (confirmed with Duane). That scopes the
1.0 target: the Gradle frontend must be solid; Maven/CMake/Make frontends and mixed
Java+Kotlin modules are real but deferred (see §9).

## 2. The shape: a compiler, not a translator

The onboarder is a **compiler frontend/IR/backend split**, for one reason: the
expensive part (turning a resolved build graph into idiomatic brix) should be written
**once** and amortized across every source build system.

```
  build tool          tool-neutral            brix
  ──────────          ────────────            ────
  Gradle  ─┐
  Maven   ─┼─ dumper ─▶  IR (JSON) ─▶ emitter ─▶ brix_build.py
  CMake   ─┘  (frontend)  (§4)        (backend)
```

- **Dumper (frontend), one per source tool.** Introspects the incumbent build's
  *already-resolved* model and writes the IR. There is **no build-file parsing** — see
  §3.
- **IR (the seam).** A JSON document shaped by *brix's* needs (modules + task nodes),
  not by any one tool. It is what makes the emitter tool-neutral and what lets the
  emitter be tested against captured fixtures with no build tool present.
- **Emitter (backend), written once.** Reads the IR, maps recognized tasks to native
  brix atoms, and bridges the rest back to the source tool. Only the emitter knows
  brix's authoring API; a new frontend never touches it.

Precedent for the seam: `compile_commands.json` (one compiler-neutral fact file that
many tools consume). The IR is that idea for a whole build graph.

## 3. Dumper principle: introspect the resolved model, never parse build files

`build.gradle` is a *program*, not a manifest. Its multi-level indirection —
`subprojects {}`, `buildSrc/` convention plugins, variants, version catalogs — only
resolves to concrete facts when Gradle *runs its configuration phase*. So the dumper
does not read text; it runs inside Gradle and transcribes what Gradle already computed.

Implementation (`src/brix/onboard/gradle/`):

- `dump.gradle` is an **init script** injected with `--init-script`. It registers one
  task, `brixDumpModel`, and walks `root.allprojects` to emit the IR.
- The walk runs in the task's `doLast` — i.e. at **execution phase**. This is not a
  detail: resolving a configuration's classpath during the *configuration* phase throws
  "resolution attempted without an exclusive lock". Execution phase is the only place
  classpath resolution is legal.
- Classpaths come from `cfg.incoming.artifactView { it.lenient(true) }.artifacts`, so a
  single unresolvable dependency degrades to a missing entry instead of aborting the
  whole dump.
- `runner.py` shells out to the project's own `gradlew` (which pins the Gradle version
  the build expects) and reads back the IR JSON. **No JVM is embedded in Python** — we
  let Gradle do the extraction only it can do, and cross the boundary with a file. This
  keeps the Python side dependency-free and works on the air-gapped box where Gradle
  already runs.

## 4. The IR

One document per dumped build. `ir_version` is bumped on any shape change; the emitter
requires `source_tool == "gradle"` today (other tools need their own classpath adapter,
§9). Validated by `_validate_ir` in `tests/onboard/test_gradle_dumper.py`.

```
{
  "ir_version": 1,
  "source_tool": "gradle",
  "gradle_version": "...",
  "root": "<abs path to build root>",
  "modules": [ Module, ... ],
  "nodes":   [ Node,   ... ]
}
```

**Module** — one per Gradle project:

| field            | meaning |
|------------------|---------|
| `path`           | Gradle project path (`":"`, `":service-a"`) — the identity used everywhere |
| `name`           | short name (`app`) |
| `project_dir`    | absolute directory |
| `build_file`     | absolute path or `null` |
| `source_sets`    | list; each `{name, java_dirs, kotlin_dirs, resource_dirs, output_dirs, generated_dirs}` |
| `dependencies`   | `{configuration → [resolved jar paths]}` (compile/runtime/test…) |
| `project_deps`   | Gradle paths of project-to-project deps (Gradle may list a module as its own dep — the emitter filters this, §6) |
| `java_toolchain` | the required Java language version as an int (e.g. `17`), or `null` — *portable*, so the emitter can bake it in; the JDK **path** stays a per-machine env var |

`generated_dirs` are source-set `srcDir`s that live under the module's build dir — a
codegen task's output wired into the source set. Captured **existence-agnostic**: on a
clean checkout they do not exist yet, but the emitter must still glob them (§6). This is
distinct from the other `*_dirs`, which are filtered to what exists on disk (so the
emitter never globs/jars a declared-but-absent `src/main/resources`).

**Node** — one per Gradle task:

| field        | meaning |
|--------------|---------|
| `id`         | task path (`":app:compileJava"`) |
| `module`     | owning module path |
| `task_name`  | `compileJava`, `bootJar`, … |
| `type`       | de-decorated task class (`...JavaCompile`; Gradle's `_Decorated`/`$$Lambda` suffixes stripped) |
| `inputs` / `outputs` | declared absolute paths |
| `depends_on` | task-graph edges (task paths) |
| `recognized` | `true` if `type` is one the emitter maps natively (drives §5); never affects correctness |
| `fidelity`   | `authoritative` if recognized **or** it declares outputs; else `verify` (a node with no declared outputs and an unknown type — the emitter cannot trust it blind; reserved for a future double-build-and-diff pass) |
| `bridge`     | tool-neutral descriptor `{tool, target}` — how to reproduce this node by invoking the source tool for it |

The `fidelity` and `bridge` fields are in the IR (not invented by the emitter) precisely
so a *future* frontend and the verify pass work identically across tools.

## 5. Emitter: native where understood, bridge everywhere else

`emit_build_file(ir, *, root_env_var="BRIX_PROJECT_ROOT", gradle_env_var="BRIX_GRADLE")
-> EmitResult` (`src/brix/onboard/emit.py`). `EmitResult` carries the rendered
`source`, human-facing `notes` (the gaps + wiring decisions), and
`emitted_modules`/`skipped_modules`/`bridged_nodes`.

It runs in two passes (needed so cross-module wiring can reference *any* sibling that
will be built, regardless of module order):

1. **Plan** every module that has a compile task and a `main` source set — its slug, jar
   path, source glob, generated globs, toolchain, `project_deps`, Spring Boot flags.
2. **Render** each plan: the native spine, plus sibling wiring / generated-source globs /
   a Spring Boot fat jar as applicable; then the bridge section.

### The native spine

Per module with a compile task, one `java`/`kotlin` `JarAction`. Its classpath is
**delegated to `java.gradle(...)`** — the sanctioned migration path: the dependency
*declarations* stay in the build files, brix drives Gradle to resolve them and owns
compilation and incrementality. The emitter does not freeze a jar list into the build
file. Language is chosen per module by task type (`_module_language`): KotlinCompile →
`kotlin`, else JavaCompile → `java`.

Two facts the IR genuinely cannot carry are emitted as explicit `# TODO`s and recorded
in `notes`, never guessed: the **main class** (`entry_point`) and Kotlin **compiler
plugins** (e.g. Spring all-open, `spring_allopen=`). This is the onboarder-surfaces-gaps
loop working as intended.

The **toolchain** is bridged portable-fact/machine-fact: the IR carries the version
(`java_toolchain`), the emitted file reads the JDK *path* from `$BRIX_JAVA_INSTALLATIONS`
and passes it via `java.gradle(java_installations=...)`, and the version is baked into a
comment/note so a human knows which JDK to point it at.

### Bridges (`_bridge_candidates`, `_emit_bridge_block`)

Any node that is not recognized, is not part of the spine (`_SPINE_SUFFIXES`), is not an
environment/IDE task (`_BRIDGE_TYPE_BLOCKLIST` — `Wrapper`, `UpdateDaemonJvm`,
Eclipse/IDEA generation; running these as build steps is wrong), and produces at least
one in-root output, is emitted as a cached `ExternalProcessAction` that re-invokes the
source tool for exactly that target (`{gradle} --console=plain -q :module:task`).

The safety argument for a bridge's inputs is the load-bearing idea:

- Inputs are a **coarse over-approximation** — the module's source/resource dirs and its
  build file, **never the build-output dir** (which would create an output→input cycle).
- Over-approximation is **safe**: it makes brix re-invoke the tool more often than
  strictly necessary, but it can **never serve a stale output**. Under-approximation
  would be the unsafe direction, so we deliberately err coarse.
- Outputs are captured file-or-directory: a directory output becomes `FileData(dir)` plus
  a recursive `glob(dir/{name})` — the same idiom `JavaCompile` uses to capture class
  files it cannot enumerate in advance.

The one input hole this leaves is a generator that reads a **sibling module**; that is
the cross-module bridge-input gap, still open (§9).

## 6. The three wiring rules the spine adds

These are where the emitter stops being a transcription and starts being a build.

**Cross-module sibling jars.** For each `project_dep` that brix *also* builds, the
emitter puts that sibling's `build/brix/<slug>.jar` on the dependent's compile classpath
(the skin ABI-lenses it — jvm-abi for Java, kotlin-abi for Kotlin — so a sibling
recompiles a dependent only on an API change) and **subtracts** Gradle's stale copy of
that sibling's output from the resolved classpath (`java.gradle(...) - ['*/<dir>/build/*']`).
Self-deps (Gradle can list a module as its own `project_dep`) and duplicates are dropped.

**Generated sources.** Each `generated_dirs` entry is globbed alongside the primary
sources (`data.glob(src) + data.glob(generated)`). The producing codegen task is emitted
as a bridge; brix then infers the **ordering edge from data overlap** — the bridge
produces `glob(dir/{name})`, the compile consumes `glob(dir/{name}.java)`, so the
generator is scheduled first and pulled in automatically. No explicit dependency is
declared; it falls out of brix's edge inference.

**Spring Boot fat jar.** When a module runs the Boot plugin (a `bootJar` task,
`_is_springboot`), the emitter repackages its thin jar into an executable one via
`springboot.SpringBootJar`, so `java -jar` alone boots it. `spring-boot-loader-tools` is
pinned to the version read straight off the classpath (`spring-boot-<v>.jar`,
`_spring_boot_version`) — no human input for the version; the runtime classpath comes
from Gradle; only the `@SpringBootApplication` main class is a human-supplied
(env-overridable) constant.

### The builder-stack rule (a correctness constraint, not a style choice)

A generated build normally uses the atomic overlay. But **the moment a foreign tool
(javac/kotlinc) reads a brix-produced file from the real filesystem** — a sibling jar via
`-classpath`, or generated sources via a glob — the atomic overlay must be **off**,
because staging would hide the freshly built file from that tool (the parked
"staged-overlay invisible to foreign resolvers" limitation, see roadmap). So the emitter
selects a plain in-place `CacheBuilder(IncrementalBuilder(...))` whenever there is a
sibling edge *or* a generated-source edge, and the atomic
`AtomicBuilder(CacheBuilder(IncrementalBuilder(...)))` otherwise. This is the same rule
the hand-authored `examples/kotlin_multimodule/` build follows.

## 7. Entry points

- `python -m brix.onboard <project>` — **the front door.** Dump → emit → `brix_build.py`
  in one command, then print the gaps to fill and the wiring taken. Writes
  `<project>/brix_build.py` by default with a **fail-fast overwrite guard** (checked
  before the expensive dump; never clobbers an existing build file without `--force`).
  `--ir PATH` keeps the intermediate IR; otherwise it goes to a temp file and is
  discarded. Reusable `run_onboard(...)` with an `ir=` hook makes the write/guard logic
  testable without Gradle (`src/brix/onboard/__main__.py`).
- `python -m brix.onboard.gradle <project>` — dump only (→ IR JSON), for debugging.
- `python -m brix.onboard.emit <ir.json>` — emit only (from a saved IR), for debugging.

## 8. How it is proven

- **Dumper:** spring-petclinic-kotlin (single module, full classpaths) and junit5 (25
  modules, 2271 nodes, 109 cross-module edges; bespoke generators captured as bridges).
- **Emitter, live end-to-end:** the generated petclinic build compiles the module, warm
  rebuild replays from cache, and — after filling only the flagged TODOs — the jar
  **boots the real Spring Boot app**; its **fat jar boots via `java -jar` with no
  classpath** (76 MB, 142 nested libs).
- **Cross-module:** a synthetic `core`←`app` build — brix builds `core`, compiles `app`
  against brix's `core.jar`, and the ABI edge is correct both ways (comment-only edit →
  `app` replays; new public method → `app` recompiles).
- **Generated sources:** a synthetic pure-Java codegen build — the generator bridge runs,
  `app` compiles against its output, and changing what the generator emits recompiles the
  consumer.
- **Fixtures + unit tests** (`tests/onboard/`) pin the IR schema and every emitter branch
  with no Gradle present; the live tests skip unless a local Gradle project + JDK are
  available.

## 9. Deferred, with the reason

- **Mixed Java+Kotlin modules.** The spine classifies a module as one language and globs
  one primary source root. A module with both `.java` and `.kt` (junit5's
  `junit-jupiter-api`: 166 `.java` + 1 `.kt`) under-compiles; it needs a chained
  kotlinc→javac spine. *Deferred: the real driver is pure-Java Gradle, so this is a
  junit5-proof concern, not a 1.0 blocker.*
- **Generator reading a sibling module.** The coarse per-module bridge inputs (§5) do not
  capture a generator that reads another module's sources — the remaining cross-module
  bridge-input hole.
- **Coalesce concurrent bridge invocations.** Many bridges firing parallel `gradlew`
  calls can contend on the Gradle daemon; batch or serialize. Opportunistic — a
  pure-Java target is mostly native spine.
- **More frontends (Maven/CMake/Make).** Each is a new dumper emitting this same IR; the
  emitter is unchanged except for a per-tool classpath adapter (today
  `emit_build_file` hard-requires `source_tool == "gradle"` for the `java.gradle()`
  path). **Not a 1.0 blocker** — the real monorepo's Java side is Gradle.

## 10. Invariants for anyone extending this

- **The IR is the contract.** A new frontend produces this schema; it does not get to
  add tool-specific fields the emitter must special-case. Shape changes bump
  `ir_version`.
- **Correctness over speed in bridges.** Keep bridge inputs coarse/over-approximate;
  never narrow them to the point where a change could be missed. Re-running too much is
  acceptable; a stale output is not.
- **Never guess a semantic gap.** A fact the source model cannot carry (main class,
  compiler plugin) is a `# TODO` + a note, never an inferred value.
- **Portable fact in the IR, machine fact in an env var.** Toolchain version travels in
  the IR; the JDK path is `$BRIX_JAVA_INSTALLATIONS`. Follow this split for any new
  machine-specific input.
- **Atomic off when a foreign resolver reads a brix output** (§6). Any new wiring that
  makes an external tool read a brix-produced file from disk must keep the in-place
  builder.
