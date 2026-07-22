# Codex Evaluation of Brix

## Overall impression

Brix is very promising. It feels like a real build system, not just Make rewritten in Python.

Its strongest idea is that build steps describe the files and data they use, and Brix works out what must run and in what order. It then adds the features expected from a serious build system: incremental rebuilding, shared caching, parallel execution, atomic updates, persistent workers, and explanations of why something needs to rebuild.

The implementation also appears more mature than the `0.1.0` version suggests. The project has a large test suite, a Rust engine that is checked against the Python reference implementation, and examples based on substantial real-world projects.

## What Brix does especially well

### It handles difficult correctness cases

Brix accounts for cases that simpler build tools often get wrong:

- A source file is deleted.
- A command fails after producing some output.
- A generated collection gains or loses members.
- A compiler discovers extra dependencies, such as C/C++ headers.
- A cached result needs to be restored into an atomic staging area.
- A build is interrupted while committing its results.

These details matter. A fast build system is not useful if it occasionally leaves old or incomplete output behind.

### It can understand meaningful changes

The `jvm-abi` and `ts-dts` content identities are important features. They let Brix distinguish between a change to implementation details and a change to a public interface.

For example, changing the body of a Java method should not require every module that uses that library to be recompiled if the public Java API stayed the same. This can save much more time than ordinary file-based change detection.

### The Python and Rust split makes sense

Python is a friendly and flexible language for writing build definitions. Rust is a good choice for the engine that performs planning, hashing, scheduling, caching, and execution.

The project does not merely claim that the two engines behave the same. It checks them against shared fixtures and compares important results byte for byte. That makes the Rust rewrite much safer.

### Real-project testing is excellent

Testing Brix against projects such as JUnit, Jackson, fmt, LevelDB, Spring Petclinic, and the Silver Searcher is one of the best parts of the development approach.

Small examples prove that an API works. Real projects reveal missing features, unclear assumptions, performance problems, and correctness bugs. The README shows that this dogfooding has already found several real issues.

### The design has clear safety rules

The project consistently favors a safe rebuild over an incorrect cache hit. It also separates scheduling state from the graph itself and uses one controlled path for adding runtime work. Those decisions reduce the chance of subtle ordering bugs.

## Main concerns

### The project is difficult to understand quickly

The main README contains a great deal of valuable information, but it is doing too many jobs at once. It is currently a user introduction, technical specification, roadmap, development history, benchmark report, and handoff document.

A new user has to read through a lot of implementation history before forming a simple picture of Brix. The project needs a shorter front door that answers:

- What problem does Brix solve?
- Why would I choose it instead of Make, Ninja, Gradle, or Bazel?
- What does a normal Brix project look like?
- Which features are ready to depend on today?
- What remains experimental?

The detailed history should remain available, but it should live in separate development or design documents.

### The public API looks larger than necessary

The top-level Python package exports both authoring tools and engine details. Most users probably need `Graph`, data expressions, actions, builders, reporters, and the language helpers. They probably do not need execution nodes, node state tables, or other scheduler concepts in the main API.

Once users build extensions against an internal class, changing it becomes difficult. Brix should clearly separate its supported public API from advanced or internal APIs before it gains more users.

### Two actions must not silently own the same output

If two build steps can write the same file, the result may depend on which one finishes last. That makes the build unreliable and difficult to debug.

This should normally be a graph validation error rather than only a warning. An explicit escape hatch can be added if a legitimate case is found, but safe ownership should be the default.

### Explicit ordering is still needed

Most dependencies should be expressed through files and data. That is one of Brix's strengths. Some tasks, however, need ordering without exchanging a file—for example, starting a service before running integration tests.

Brix needs a clear way to express “run B after A.” It should be visible as an ordering-only relationship so users do not mistake it for a data dependency.

### Automatic behavior must remain explainable

Automatic dependency inference is convenient, but an inferred dependency can be harder to understand than an explicitly written one. Users need to be able to answer:

- Why did this action run?
- Why was it skipped?
- Which input changed?
- Why does Brix believe these two actions are connected?
- Why was this cached result accepted or rejected?

The existing `status` and `graph` work is a strong start. Clear explanations should remain a central product feature, not only a debugging aid.

### Python graph creation can depend on the surrounding machine

Ordinary Python allows a build file to inspect environment variables, the filesystem, the network, installed packages, and the current time while creating the graph. That flexibility is useful, but it can also make two machines construct different graphs unexpectedly.

Brix does not necessarily need to forbid this. It does need to document what predictable graph creation means and how users should declare configuration that is allowed to affect a build.

### A few known boundaries remain

- On a fresh machine, depfile-based C/C++ header tracking cannot provide a fully header-aware cache key until the compiler has produced the depfile once.
- Foreign tools such as Maven may not understand Brix's atomic staging view.
- The future TypeScript API will test whether the Rust boundary is truly language-neutral or still shaped around Python assumptions.
- Removing the Python execution engine is sensible eventually, but only after the Rust default has been used for a meaningful period on real work.

## Recommended next steps

### 1. Reject overlapping output ownership

Add graph validation that detects when two actions may write the same path. Report both actions, the overlapping expressions, and an example path when possible.

Make this an error by default. Add an explicit opt-in only if a real use case requires shared ownership.

### 2. Add ordering-only dependencies

Provide a small API for expressing that one action must complete before another even when no file connects them.

The graph display and status output should label this clearly as an ordering dependency. The API should also make the behavior after failure explicit, matching the existing `on-success` and `on-completion` concepts where appropriate.

### 3. Keep soaking the Rust engine

Use the Rust engine as the default on real projects for days or weeks, not only test runs. Record any differences, hangs, unclear failures, or performance regressions.

Do not remove the Python execution engine until this period has been uneventful and the important scheduler and layer behavior is covered by tests. Keep the Python reference math and golden-fixture generator as planned.

### 4. Simplify the documentation entry point

Reduce the main README to a clear introduction and navigation page. Move detailed material into focused documents, for example:

- `docs/getting-started.md`
- `docs/concepts.md`
- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/development-log.md`
- `docs/limitations.md`

Preserve the current detailed history rather than deleting it. The goal is to make it easier to find.

### 5. Define the supported public API

List the names that ordinary build files are expected to use and treat those as the compatibility promise.

Move engine and scheduler types out of the top-level exports unless they are intentional extension points. If advanced extension APIs remain public, label their stability separately.

### 6. Improve the first-run experience

Create one short, polished path from installation to a successful incremental build. A new user should be able to:

1. Install Brix and its Rust extension.
2. Create or copy a small `brix_build.py`.
3. Run a build.
4. Run it again and see that work was skipped.
5. Change one input and understand why only part of the graph rebuilt.
6. Use `status` and `graph` to inspect the decision.

This should be tested in a clean environment as part of releases.

### 7. State the reproducibility model

Document which parts of the environment may safely affect graph creation and action fingerprints. Provide a recommended way to declare configuration, environment values, platform choices, and tool versions.

Warn about common sources of accidental differences between machines, even if Brix deliberately keeps Python graph creation unrestricted.

### 8. Finish the next dogfood cases

The planned annotation-processor example and Jackson multi-module test selection are good next targets. They test important Java behavior without requiring large engine changes.

Each dogfood project should produce a short record of what was learned: missing feature, correctness issue, performance result, or confirmation that the current design was sufficient.

### 9. Define and publish the cache trust model

Explain what makes a cache entry safe to reuse, which inputs are fingerprinted, and where the system intentionally chooses an unnecessary rebuild over a possibly stale result.

Clearly document the fresh-machine depfile limitation and any limits involving undeclared inputs, network access, or tools that read ambient machine state.

### 10. Start the TypeScript binding only after the core settles

Once the Rust engine has soaked and the public API is narrower, build a small TypeScript binding proof of concept before implementing the full surface.

Use it to test the language boundary: graph serialization, reporter callbacks, errors, cancellation, threading, custom extensions, and naming conventions. Any Python-specific assumptions found at this stage should be fixed in the shared boundary rather than hidden in the TypeScript wrapper.

## Suggested description

A plain-English description of Brix could be:

> Brix works out what needs to run from the files and data each step uses. It avoids unnecessary work, explains its decisions, shares cached results, and prevents half-finished builds from changing the project.

That is a stronger and more distinctive message than simply saying that build graphs are written in Python.

## Final assessment

Brix has a strong technical foundation. Its most valuable qualities are correctness, semantic change detection, composable build guarantees, and careful testing against both a reference engine and real projects.

The next challenge is simplification. The project should narrow and clearly label its public surface, give new users a shorter path into the documentation, close ambiguous output ownership, and keep explanations central to the experience.

If Brix can make its advanced behavior feel simple, it could fill a useful space between Make, which is small but limited, and Bazel, which is powerful but asks users to adopt a much larger system and way of working.
