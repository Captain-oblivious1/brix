# C++

Typed C/C++ actions — compile, archive, link — plus a `pkg-config` resolver.
They lower to the generic action model, so they compose with any other language
in one graph, and compilation automatically tracks `#include` headers through
the compiler's depfile. `DoxygenAction` runs `doxygen` over a project's own
sources as a tracked, incremental step.

::: brix.cpp
    options:
      show_root_heading: false
      show_root_toc_entry: false
