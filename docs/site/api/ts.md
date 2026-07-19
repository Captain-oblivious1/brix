# TypeScript

Typed TypeScript actions built on `tsc`, with the **ts-dts** lens (`dts`) so a
dependent recompiles only on a public-declaration change, plus automatic import
tracking (a `--listFiles` depfile) so editing a source a module imports —
even outside its own tree — re-stales it. `TypedocAction` runs `typedoc` over
a project's own sources as a tracked, incremental step.

::: brix.ts
    options:
      show_root_heading: false
      show_root_toc_entry: false
