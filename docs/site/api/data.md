# Data model

Every action declares its inputs and outputs as **data expressions**: concrete
files, globs, values, and tools. Brix infers dependency edges from where these
overlap, so the data model is how you tell Brix what a step touches. In build
files you usually construct them through a graph's `data` factory
(`g.data.file(...)`, `g.data.glob(...)`), documented here as
[`DataFactory`](#brix.data.DataFactory).

::: brix.data
    options:
      show_root_heading: false
      show_root_toc_entry: false
