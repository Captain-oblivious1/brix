# Core & builders

The graph and action model you assemble, the builder/layer stack that runs it,
and the reporters, results, and workspace loaders around it.

::: brix
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members:
        - Graph
        - ExternalProcessAction
        - ExpandAll
        - Expandable
        - EdgeTrigger
        - CleanBuilder
        - IncrementalBuilder
        - CacheBuilder
        - AtomicBuilder
        - NestableBuilder
        - build_stack
        - BuildLayer
        - NodeContext
        - NodeProposal
        - Reporter
        - ConsoleReporter
        - ErrorsAtEndReporter
        - BuildResult
        - ProcessResult
        - ActionStatus
        - StatusReport
        - compute_status
        - Workspace
        - load_workspace
        - load_affected
        - WorkerSpec
