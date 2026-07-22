# ChatGPT changes to Brix

This document summarizes the work done on the `chatGPT` branch. The branch was
created off `master` at commit `9f1efbb` ("P0 uniformity guard") and adds the
commits listed in the ledger at the end (July 13–15, 2026). It groups the
changes by outcome rather than by edit order.

## Scope and attribution

Everything below is what **this branch added on top of that `9f1efbb` base**.
The base already contained a large amount of prior work — `content_key`
comparison traits, `DepSet` with the `java.pom` / `java.maven` / `java.gradle`
and `cpp.pkg_config` resolvers, Spring Boot executable jars, `brix.ts` and the
`ts-dts` lens, glob subtraction, the capability-sufficiency audit, the atomic
tool-self-resolution guard, and the scaling benchmark. That work is *not* part
of this branch and is not claimed here; it is the context the evaluation was
written against (see "The base this branch reviewed" below).

## Evaluation and priorities

I reviewed the existing Brix base as a fresh implementation and wrote the
findings in `docs/codexEvaluation.md`, later rewriting the evaluation in plainer
English. The review found that Brix already had a strong technical base: careful
incremental correctness, semantic Java and TypeScript change detection, a Rust
execution engine checked against a Python reference, and unusually good
real-project dogfooding.

The main recommendations were to make output ownership unambiguous, support
ordering-only graph edges, simplify the documentation, narrow the public API,
and improve installation — the items this branch then implemented.

## Graph correctness and ordering

### Conflicting output ownership

Brix now rejects graphs in which two actions could own the same output. The
validation exists in both the Python and Rust planners and reports the actions
and overlapping expressions involved. Tests cover concrete files, globs,
regular expressions, expanded actions, false positives, and Python/Rust parity.

This closes a correctness hole where the final output could otherwise depend on
which action happened to finish last. The rule is now recorded as design rule
14 in `docs/specification.md`.

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
I also kept related design documents and example READMEs in sync.

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

## The base this branch reviewed

For context, these capabilities were **already present on `master`** when the
branch was created and are not this branch's work. They are listed only so the
evaluation and priorities above make sense:

- the `content_key` comparison traits and lens roster;
- `DepSet` and the `java.pom` / `java.maven` / `java.gradle` resolvers;
- `cpp.pkg_config` and glob subtraction (`-`), with the Silver Searcher and
  LevelDB dogfoods;
- Spring Boot executable jars (`brix.springboot.SpringBootJar`) and the
  Petclinic dogfood;
- `brix.ts` / `ts.CompileAction` and the `ts-dts` lens;
- the incremental-equivalence oracles (exhaustive, ABI, and depfile);
- the capability-sufficiency audit and the atomic tool-self-resolution guard;
- the scaling benchmark and its corrected (release-build) conclusions;
- the parked designs (filesystem watcher, staged-overlay visibility) and the
  future `brix_build.py help` specification.

This branch made only minor follow-on doc edits to a few of these (e.g.
`docs/capability-audit.md`) while landing the changes above.

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

The principal next usability item is the live `brix_build.py help` command specified
in the roadmap. Other important remaining work includes documenting the
reproducibility and cache-trust models, continuing real-project soak of the Rust
engine, and delaying removal of the Python execution internals until that soak
has been uneventful.

Larger ideas — sandboxing/staged-overlay views, a resident filesystem watcher,
remote execution, and a TypeScript authoring binding — remain intentionally
parked or scheduled after the current API and engine have settled.

## Commit ledger

These are the commits this branch adds on top of the `9f1efbb` base.

### July 15

- `4ce40f0` — Add `docs/chatGptChanges.md`.

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
