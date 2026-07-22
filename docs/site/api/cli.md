# Command line

A project's `brix_build.py` exposes a command-line interface by calling `cli.main`.
It provides `build`, `status`, `clean`, `help`, and related subcommands, plus
project-defined target aliases and build-then-act commands.

::: brix.cli
    options:
      show_root_heading: false
      show_root_toc_entry: false
