# Extending brix

Brix has three plug-in seams. Each is an injected object you pass to a builder,
so a project can swap in its own behavior without touching the engine.

## Change detection

How Brix decides whether a file changed. The default stats and hashes on demand;
`GitChangeDetector` accelerates a git working tree by trusting git's own view of
what is unmodified. It accelerates both engines (see `docs/roadmap.md`).

::: brix
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members:
        - GitChangeDetector

## Result caching

Where built results are stored so they can be restored — across workspaces or
across a team. Implement `CacheStoreProtocol` to back the cache with your own
storage.

::: brix
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members:
        - CacheStore
        - CacheStoreProtocol
        - HttpCacheStore
        - TwoTierCacheStore
        - DepSet

## Content lenses

How Brix judges whether a change actually *matters* to dependents — comparing a
file by meaning, not bytes. Register a `ContentIdentity` to add your own lens; the
built-in `jvm-abi`, `kotlin-abi`, and `ts-dts` lenses are selected by the typed
helpers (`java.abi`, `kotlin.abi`, `ts.dts`).

::: brix.identity
    options:
      show_root_heading: false
      show_root_toc_entry: false
