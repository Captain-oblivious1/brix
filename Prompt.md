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

First task: generate class diagrams for every class in src/brix/ (Duane is
a visual person; SVG or Mermaid, added to docs/, verified renderable —
use rsvg-convert or headless Vivaldi for screenshots, NOT Firefox). Then
proceed with the roadmap in order (1. pluggable cache store, 2. JVM
fast-builds phases 1–2, 3. Rust port phases 1–2), pausing at anything the
docs flag as needing full-strength review (Rust phase 3, JVM phase 3).

Real-world test project: ~/Projects/animation (run /add-dir first). Its
brix.py uses the full stack; build with venv/bin/python brix.py --dryRun
build. Its output/ holds dryRun stubs. brix is editable-installed in its
venv.
