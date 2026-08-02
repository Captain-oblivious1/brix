# Shared / remote cache (Nexus, Artifactory, S3, …)

brix's result cache is content-addressed by the same `action_fingerprint` the
incremental layer uses, so a hit is exactly as trustworthy as re-running the
action. Point that cache at a **shared** store and one developer's (or CI's)
compile becomes everyone's cache hit — a clean checkout on a fresh machine
reconstructs the whole build without recomputation.

Because fingerprints are **machine-portable** (helpers are addressed by content,
not by a local path — see [asset-engine.md](asset-engine.md)), two machines
compute the *same* key for the same logical step, which is what makes a shared
cache hit across machines at all.

## The backend

`HttpCacheStore` (`brix.HttpCacheStore`) is a `CacheStoreProtocol` store over
plain HTTP `GET`/`PUT`, stdlib-only (no new dependency). Any server that honors
`GET`/`PUT` by path works: **Nexus** (a *raw hosted* repository), Artifactory, an
S3-compatible gateway, or a plain file server. The on-disk/on-wire layout mirrors
the local `CacheStore` (`<fp[:2]>/<fp>/entry.json` + `files/<n>`), so a local
directory and a remote one are the same shape and can be synced into each other.

```python
from brix import HttpCacheStore

remote = HttpCacheStore(
    "https://nexus.example.com/repository/brix-cache",   # a raw hosted repo
    auth=lambda: {"Authorization": "Basic " + token()},  # called per request
    retries=2,
    timeout=30.0,
)
```

The `auth` hook returns request headers and is called **per request**, so
short-lived tokens can refresh. Basic and Bearer both work — it is just a header.

## Recommended production wiring: two tiers

Put a fast local directory cache **in front** of the shared remote. Local hits
cost no network round-trip; a remote (back) hit serves the build and is
*promoted* into the local front, so the next local build hits without touching
the remote. A write populates both, best-effort on the remote.

```python
from brix import CacheBuilder, IncrementalBuilder, CacheStore, TwoTierCacheStore

builder = CacheBuilder(
    IncrementalBuilder(create_graph, root=ROOT),
    store=TwoTierCacheStore(
        CacheStore("build/.brix-cache"),   # local front
        remote,                            # shared back
    ),
)
```

This works identically under the default Rust engine and the Python engine — the
store's methods cross the boundary as the `cache_store` extension trait and the
core calls back into them per node. Proven cross-machine at scale in
`tests/test_cache_http.py` (a clean machine reconstructs a many-target build from
the remote with zero recomputation).

## Resilience

All robustness lives in the cache *layer*, not the store: any store error — an
unreachable remote, a permission failure, a corrupt entry — degrades to a **cache
miss** (or a skipped populate), never a failed build. A down remote just means
"run the work," so wiring a shared cache never makes a build more fragile than no
cache at all.

## Operational notes

- **Idempotent, content-addressed writes.** A blob's path *is* its content, so
  re-uploading is a no-op you *want* to succeed. On a Nexus raw repo, either
  enable **redeploy** on the repository, or accept that a re-`PUT` of an
  already-present object returns an error the cache layer swallows harmlessly
  (the content is already there — nothing is lost).
- **Blobs first, entry last.** `store` uploads all content blobs before the
  `entry.json`, so a partial upload reads back as a clean miss, never a false hit.
- **Retention is the server's job.** brix does not evict the remote; set the
  repository's own cleanup policy (age/size) on Nexus/Artifactory. The *local*
  front tier is bounded with `CacheBuilder(max_bytes=…, max_age=…)` — see the
  roadmap's cache-eviction entry.
- **Auth per request.** Return fresh headers from the `auth` hook so rotating or
  short-lived credentials refresh without rebuilding the store.
