# ChatGPT changes to Brix

This document summarizes the work completed on the `chatGPT` branch from
July 10 through July 14, 2026. It groups the changes by outcome rather than by
the order in which individual files were edited. The commit ledger at the end
provides the exact history.

## Evaluation and priorities

I reviewed Brix as a new implementation and wrote the findings in
`codexEvaluation.md`, later rewriting the evaluation in plainer English. The
review found that Brix already had a strong technical base: careful incremental
correctness, semantic Java and TypeScript change detection, a Rust execution
engine checked against a Python reference, and unusually good real-project
dogfooding.

The main recommendations were to make output ownership unambiguous, support
ordering-only graph edges, simplify the documentation, narrow the public API,
improve installation, continue real-project testing, and keep soaking the Rust
engine before deleting the Python implementation.

I also performed a capability-sufficiency audit. That audit separated missing
features from cases already expressible through action composition and recorded
larger designs that should remain parked until a real project justifies them.

## Graph correctness and ordering

### Conflicting output ownership

Brix now rejects graphs in which two actions could own the same output. The
validation exists in both the Python and Rust planners and reports the actions
and overlapping expressions involved. Tests cover concrete files, globs,
regular expressions, expanded actions, false positives, and Python/Rust parity.

This closes a correctness hole where the final output could otherwise depend on
which action happened to finish last.

### Explicit ordering edges

I added `Graph.force_order(before, after, trigger=...)` for cases where one
action must finish before another even though no file connects them. Ordering
edges are carried through lowering, planning, target selection, Rust boundary
serialization, graph descriptions, and status reporting.

The trigger is the typed `EdgeTrigger` enum, so build files get editor
completion and avoid fragile string values. It supports ordering after success
or after completion, matching the scheduler's existing failure behavior.

## Public API and documentation

### Smaller Python v1 API

I defined the supported top-level Python API and removed engine implementation
details from `brix.__init__`. Public authoring types, results, reporters,
builders, workers, cache extension points, and language helpers remain easy to
import. Internal scheduler and storage classes remain available from their
implementation modules without being presented as the compatibility promise.

`docs/python-api.md` records this boundary, and `tests/test_public_api.py` pins
the intended exports. Because Brix has no outside consumers yet, this was done
directly without a deprecation bridge.

### Documentation split

The former very large README was divided by purpose:

- `README.md` is now the short introduction and navigation page.
- `docs/getting-started.md` contains the first-run path.
- `docs/specification.md` contains the behavioral and graph model.
- `docs/roadmap.md` contains unfinished, parked, blocked, and undecided work.
- `docs/development-log.md` preserves the detailed implementation history.
- `docs/python-api.md` defines the supported Python surface.
- `docs/packaging.md` explains installation and release artifacts.

No historical material was discarded; it was moved to a more appropriate home.
I also kept related design documents and example READMEs in sync as features
landed.

### Live-help design, not yet implemented

I wrote down the intended behavior for a future `brix.py help` command. It
should show the active builder stack, aliases, custom commands and their
docstrings, metadata/cache/staging paths, and whether atomic staging operations
apply. This remains roadmap work; the documentation commit did not implement
the command.

## Installation and packaging

I replaced the previous setuptools arrangement with a Maturin mixed
Python/Rust build. A built wheel now contains:

- the `brix` Python package;
- the compiled PyO3 extension as `brix.brix_py`;
- the Java worker and Spring Boot repackager sources;
- an installed `brix-workerd` launcher backed by the Rust daemon code.

The daemon was refactored into a reusable Rust library plus a small binary, and
the Python extension exposes its entry point for the installed console script.
Engine discovery now finds the installed launcher next to the active Python
executable, so it does not depend on an activated shell or a development target
directory.

I added `scripts/wheel_smoke.py`, which builds a release wheel, creates an
isolated virtual environment, installs the wheel without using the checkout,
runs a real Rust-backed Brix build, checks the bundled Java sources, verifies
daemon discovery, and exercises the installed launcher. Editable Maturin
installation and a wheel rebuilt from the source distribution were also
verified.

## Java build improvements

### Spring Boot executable jars

I added `brix.springboot.SpringBootJar` and a dependency-free Java
`BootRepackager` helper. The Spring Petclinic dogfood now compiles its real
sources, packages resources, produces an executable Spring Boot fat jar, and
boots with `java -jar`.

### Maven and Gradle dependency adoption

I introduced `DepSet` and Java dependency helpers that let Brix drive an
existing resolver while continuing to own compilation and incrementality:

- `java.pom(...)` adopts dependencies declared in a Maven POM.
- `java.maven(...)` resolves coordinates declared in a Brix build file.
- `java.gradle(...)` adopts a Gradle configuration through an injected init
  script without editing the project.

Resolution is cached from the relevant build-file content, and resolved jars
become normal tracked Brix inputs. Maven and Gradle remain explicitly named
because their conflict-resolution rules differ.

### Annotation processors

`java.JarAction` now accepts `processorpath`. The persistent javac worker joins
those entries into a separate `-processorpath` argument.

Processor jars are fingerprinted by their complete bytes rather than by their
public Java ABI. This is important because a processor's implementation can
generate different code without changing its own public signatures.

Unit tests and a real-JDK integration test prove that changing only processor
behavior recompiles the application and produces the new generated result. The
self-contained `examples/annotation_processing/` project builds a processor,
generates Java source, packages the application, and runs it.

### Multi-module Jackson test selection

I expanded the Jackson dogfood from one repository to the three real Jackson
2.19.4 core modules:

- 46 jackson-annotations production sources;
- 135 jackson-core production sources;
- 480 jackson-databind production sources.

The graph builds all three jars and runs one focused JUnit suite per module.
`examples/jackson/verify_selection.py` makes a reversible, ABI-neutral private
method change in `JsonFactory` and verifies the exact selection result:

- core compilation runs;
- the core jar is rebuilt;
- core tests rerun;
- databind tests rerun;
- databind itself is not recompiled;
- the affected test classes are not recompiled;
- the complete annotations lane replays.

This required no engine change. It confirmed that ABI-based compile inputs and
byte-based runtime inputs already compose correctly across modules.

## TypeScript support

I added the typed `brix.ts` helper with `ts.CompileAction`, along with a
two-module TypeScript example. Cross-module declaration inputs use the existing
`ts-dts` content lens, so an implementation-only library edit can regenerate a
byte-identical declaration and let dependent modules replay, while a public
type change recompiles them.

Tests cover lowering and incremental-versus-clean equivalence. Automatic
discovery of a module's internal import graph remains future work.

## C and C++ support

I generalized `DepSet` beyond Java and added `cpp.pkg_config(...)`. It asks
`pkg-config` for compiler flags, linker flags, and library files, then exposes
those as tracked Brix inputs instead of opaque command-line text.

The feature was dogfooded by building the real Silver Searcher project: 12 C
translation units with PCRE, zlib, and lzma system dependencies, producing a
working `ag` executable.

I also added glob subtraction with the `-` operator in both engines. The LevelDB
dogfood uses it to glob the real source tree while excluding platform-specific
files instead of staging a curated source copy.

## Content identities and correctness testing

I completed the shift from the older `identity=` terminology to `content_key`,
with comparison behavior owned by Rust content-key traits. Built-in content
lenses now declare themselves through one roster instead of duplicated Python
and Rust lists.

I added incremental-equivalence oracles that compare incremental results with a
clean rebuild. The coverage was strengthened from random sampling to bounded,
exhaustive edit combinations and extended to:

- ordinary byte-based inputs;
- Java ABI-lensed dependencies;
- compiler-discovered C/C++ header dependencies.

These tests are specifically designed to catch the most dangerous build-system
failure: incorrectly declaring a stale result fresh.

## Atomic-build safety

I added a guard for tools that resolve their own dependencies from paths while
an atomic build is using a staged overlay. Such a tool can accidentally read an
old workspace file instead of a newly staged producer output. Brix now refuses
that unsafe combination unless the action explicitly supports overlay-aware
resolution.

The broader solution—giving foreign resolvers a staged filesystem view—was
documented as a parked design and connected to the future sandboxing work. It
was not partially implemented.

## Performance investigation

I added a scaling benchmark and measured graph/build overhead. The first
results were misleading because they used a debug Rust extension, so I reran
the measurements with a release build and corrected the documentation.

The corrected result is that cold no-op cost grows roughly linearly and is
noticeable but not yet urgent. A resident graph plus filesystem watcher could
eventually make no-op builds proportional to changed files, but this was
documented as parked correctness-sensitive work rather than rushed into the
engine.

## Validation performed

The changes were validated proportionally to their scope, including:

- the full Python test suite, ending at 369 passed and 4 skipped;
- the complete Rust workspace tests;
- HTTP-cache and Unix-socket worker tests outside the restricted sandbox;
- real JDK compilation and persistent-worker execution;
- real Spring Petclinic, Silver Searcher, annotation-processing, and
  three-module Jackson builds;
- isolated wheel installation and editable installation;
- rebuilding a wheel from the generated source distribution;
- documentation link and whitespace checks.

## Remaining high-priority work

The principal next usability item is the live `brix.py help` command described
above. Other important remaining work includes documenting the reproducibility
and cache-trust models, continuing real-project soak of the Rust engine, and
delaying removal of the Python execution internals until that soak has been
uneventful.

Larger ideas—sandboxing/staged-overlay views, a resident filesystem watcher,
remote execution, and a TypeScript authoring binding—remain intentionally
parked or scheduled after the current API and engine have settled.

## Commit ledger

### July 14

- `eae72f1` — Prove multi-module test selection.
- `06cad7f` — Support Java annotation processors.

### July 13

- `c7b95ca` — Package self-contained native wheels.
- `d57cc68` — Define the public Python v1 API.
- `22fc8c7` — Split project documentation by purpose.
- `fd8be18` — Type force-order triggers.
- `4862a14` — Add explicit graph ordering.
- `08e8090` — Reject conflicting output ownership and add the evaluation.

### July 12

- `9f1efbb` — Refuse unsafe tool-self-resolved staged dependencies under atomic
  builds.
- `8d468b9` — Add the capability-sufficiency audit.
- `595f6af` — Remove the obsolete global `brix` wrapper roadmap item.
- `d30b508` — Specify live `brix.py help` behavior in the roadmap.
- `0d77a71` — Record the staged-overlay visibility design for foreign resolvers.
- `40e9e3a` — Add `brix.ts`, `ts.CompileAction`, and TypeScript dogfood.
- `4c6862b` — Add Gradle dependency adoption.

### July 11

- `fb3d723` — Record the parked filesystem-watcher design.
- `4494baf` — Dogfood `cpp.pkg_config` on the Silver Searcher.
- `92842b3` — Add C/C++ dependency resolution through `pkg-config`.
- `53ca038` — Add `DepSet`, `java.pom`, and `java.maven`.
- `a7a3b06` — Correct scaling conclusions after identifying a debug-build
  measurement error.
- `afdf7a8` — Add the scaling benchmark.
- `c63055d` — Expand incremental-equivalence coverage to exhaustive, ABI, and
  depfile cases.
- `2125f17` — Add the original incremental-versus-clean oracle.
- `708a0d9` — Build the content-key registry from a lens-owned roster.
- `534d3f2` — Remove duplicated built-in content-lens registration.
- `8b7ce7a` — Add Spring Boot executable-jar support and Petclinic dogfood.

### July 10

- `25bed2e` — Correct the documented `ts-dts`/`content_key` status.
- `4f0e7f1` — Update documentation to state that Rust is the default engine.
- `634c115` — Replace `identity=` with the Rust `content_key` trait surface.
- `b63d651` — Use real-tree globbing and exclusions in the LevelDB dogfood.
- `e1f18b8` — Add glob subtraction in both engines.
