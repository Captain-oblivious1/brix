Read README.md first, then follow its documentation links. The stable behavior
and design rules are in docs/specification.md, prioritized TODOs are in
docs/roadmap.md, and completed work is in docs/development-log.md. Treat the
specification's "Design rules" as fixed.

Standing working rules (from Duane, non-negotiable):
- Discuss design questions in short back-and-forth BEFORE editing code or
  docs; only change artifacts after agreement.
- Update the roadmap, development log, specification, and README as appropriate
  for every commit.
- The 130-test suite in tests/ is the executable spec; it must stay green.
  Run: python -m pytest tests/ -q

Execution contracts — major upcoming work is already fully designed. Your
job on these is faithful implementation, not redesign. If something in a
doc seems wrong or odd, it is probably load-bearing: raise it, don't
"fix" it silently.
- docs/rust-port.md        — Rust engine port (phases, do-not-reinterpret list)
- docs/boundary-schema.md  — wire schema + TS mirror-API contract
- docs/java-fast-builds.md — persistent workers + ABI fingerprints
- docs/build-layers.html   — architecture with diagrams (open in browser)
- docs/test-impact-selection.md — test-impact selection; core + Java provider
  are built, and "The Fable slice" is the live handoff brief

Current task (handed to Fable 2026-07-23): **docs/test-impact-selection.md,
"The Fable slice"** — the escape-completeness audit for Java, then the
fingerprint/cache wiring. Take them in that order; the ordering is itself a
decision (an under-selection hole was found by argument rather than by any
systematic method, so the dependency graph is not yet known to be complete,
and sealing the cache bookkeeping around an incomplete graph buys nothing).
The C++ provider is deliberately NOT started until the audit lands, so it can
inherit whatever the audit concludes instead of repeating the same gaps in a
second language.

Real-world test project: ~/Projects/animation (run /add-dir first). Its
brix.py uses the full stack; build with venv/bin/python brix.py --dryRun
build. Its output/ holds dryRun stubs. brix is editable-installed in its
venv.
