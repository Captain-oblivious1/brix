# ChatGPT changes to Brix, part 2

This document continues `docs/chatGptChanges.md`. It summarizes the work I did
after that first report, primarily on July 21, 2026. It covers the README and
onboarding rewrite, two roadmap additions, and the repository-wide change from
`brix.py` to `brix_build.py`.

It does not claim the Kotlin, environment-isolation, hierarchical-workspace,
TypeScript dependency, Rust self-hosting, documentation-generation, or other
features committed between the two reports. Those changes were already in the
branch when this work began.

## Reworked the README around the reason to use Brix

The README now begins with the problem Brix is trying to solve instead of
starting with implementation details. The short version is:

- Developers should not have to become experts in a build system or learn a
  special-purpose language just to build their code.
- A build should be easy to describe, fast, correct, and otherwise stay out of
  the developer's way.
- Brix is a library of reusable build building blocks—thus the name “Brix”—that
  developers use from a language they already know.
- Build definitions are currently written in Python. JavaScript/TypeScript
  authoring is planned but is not supported yet.
- Python and JavaScript are also well understood by AI coding assistants, so an
  assistant can help create and maintain a build.
- The repository contains many working examples and starter templates that can
  be copied instead of starting from an empty file.

The TL;DR deliberately acknowledges that someone who does not want to study a
build system probably does not want to read a long document about one either.
The remainder of the README provides more detail for readers who want it.

## Added a plain-English feature overview

The README now explains Brix's major capabilities without leading with ABI,
fingerprinting, worker protocols, or other implementation terminology:

1. **Fast builds.** Performance-sensitive work is implemented in Rust,
   independent actions can run in parallel, and unnecessary work is avoided.
2. **Incremental builds that match clean builds.** Brix treats a correct
   incremental result as the same result a clean build would have produced.
   This includes deleting obsolete downstream artifacts, not merely rebuilding
   changed files. The goal is to remove the habit of running clean builds
   because the incremental result is not trusted.
3. **Git-inspired atomic builds.** A developer can inspect status, stage a
   complete build, and then commit or discard it without leaving the workspace
   half updated.
4. **Local and shared caching.** Work can be reused across builds, workspaces,
   developers, and CI systems.
5. **Less dependency bookkeeping.** Brix can infer ordering from action inputs
   and outputs and can discover dependencies such as C/C++ headers and
   TypeScript imports. The README carefully avoids claiming that Bazel or Buck
   literally require every dependency edge to be entered by hand; it instead
   makes the narrower, defensible comparison that those systems generally
   expect explicit target dependency declarations.
6. **Growth from a small project to a monorepo.** Build files can be placed at
   package or service boundaries, and Brix can load the affected portion of a
   large repository.
7. **Explainable and configurable behavior.** Projects compose only the layers
   they need, while commands such as `status`, `graph`, `help`, and `env`
   explain what Brix sees.

## Added a progressive onboarding path

The README now gives a complete setup path rather than presenting disconnected
API fragments:

- Install Brix into the project's Python environment.
- Create one standalone build file with `Graph`, `create_graph()`, and
  `cli.main()`.
- Run `help` to verify the project's Brix setup before adding actions.
- Replace the empty graph with a very short typed C++ example.
- Follow it with a similarly shaped Java example.
- Add `build_stack` after the first successful build to enable the recommended
  incremental, cache, and atomic layers.

The C++ example intentionally uses `brix.cpp` first because it demonstrates the
value of the typed building blocks with very little code. The README later
shows the lower-level `ExternalProcessAction` equivalent so readers can see
that the typed helpers are conveniences built on the generic action model, not
a closed system.

The generic example runs `protoc` and then compiles its generated source. This
demonstrates that Brix can run arbitrary applications, existing build tools,
and code generators, while inferred input/output relationships establish the
correct order.

Low-level material was moved into a “For the technically curious” section. It
briefly points interested readers toward the Rust engine, warm workers,
interface-aware comparisons, discovered dependencies, equivalence tests,
atomic staging, caching, and lazy monorepo loading without making those details
prerequisites for understanding the pitch.

The site landing page was brought into line with the README, and the generated
HTML documentation was rebuilt.

## Added two roadmap items

The roadmap now records two follow-up questions raised during the README work:

- Add a machine-readable JSON form of `status`, with a documented and versioned
  schema suitable for web, IDE, and CI front ends.
- Decide whether `cpp.CompileSourceAction` should expose singular
  `source_file`/`object_file` names because each expanded compiler invocation
  handles one source, or retain plural names because callers supply glob
  collections that expand through `{name}`.

These were recorded as open work rather than silently deciding API behavior in
the documentation.

## Standardized the build filename as `brix_build.py`

The initial README draft used `build.py`. That avoided Python's import-shadowing
problem with a file named `brix.py`, but `build.py` is too generic for automatic
monorepo discovery: an unrelated Python helper could accidentally be treated as
a Brix package boundary.

The final convention is `brix_build.py`. It is visibly Brix-specific, remains an
ordinary Python filename, does not shadow the installed `brix` package, and is
unlikely to collide with an existing project file.

This was implemented as a complete filename-contract change rather than a
documentation-only rename:

- Workspace discovery now looks for `brix_build.py` by default.
- Affected-package selection treats changes to `brix_build.py` as structural
  build changes requiring the safe full-load fallback.
- Workspace APIs now call the override parameter `build_file` instead of the
  obsolete `brix_file`.
- The persisted workspace-index version was increased so metadata written
  under the previous filename contract cannot be reused accidentally.
- Tests explicitly prove that unrelated `build.py` files and the old `brix.py`
  name are ignored during package discovery.
- Every checked-in example, starter template, benchmark reference, test, and
  current documentation reference was changed to `brix_build.py`.
- The old `sys.path` filtering workaround was removed. A `brix_build.py` file
  no longer shadows the `brix` package merely because its directory is first on
  Python's import path.
- CLI usage derives the displayed program name from the script that was
  actually invoked instead of hardcoding a filename.

The migration intentionally does not support both names. Brix is still defining
its v1 API and has no external adopters, and accepting both names would create
an ambiguity if a directory contained both files.

## Validation

The final filename migration was checked with:

- focused workspace discovery, affected-selection, template, CLI-help, and
  environment tests;
- the complete Python suite: **547 passed and 11 skipped**;
- a successful MkDocs rebuild;
- documentation whitespace/error checks;
- direct execution of a renamed starter template.

The 11 skipped tests were expected tool-gated integration tests: seven require
`kotlinc`, and four require TypeScript tooling (`tsc`, with one also requiring
Node). None was skipped because of the filename migration or a Brix failure.

## Commit ledger

- `c6b644f` — Rewrite README around the Brix pitch.
- `8eadad0` — Finish README onboarding improvements.
- `aefca7a` — Standardize Brix build files as `brix_build.py`.

