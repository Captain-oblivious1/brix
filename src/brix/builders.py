"""Builders: choose how the logical graph is built.

`CleanBuilder` and `IncrementalBuilder` are root builders; `CacheBuilder`
and `AtomicBuilder` wrap another builder, each contributing a `BuildLayer`
to the stack rather than transforming plans. The full stack:

    AtomicBuilder(CacheBuilder(IncrementalBuilder(create_graph)))

which stacks cache innermost, incremental around it, atomic outermost —
fresh nodes replay without touching the cache, cache-restored outputs
route through staging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .graph import Graph
from .plan import ExecutionPlan, NodeState, NodeStateTable
from .planner import plan_graph
from .process import ProcessResult

if TYPE_CHECKING:
    from .cache import CacheStoreProtocol


@dataclass
class BuildResult:
    success: bool
    plan: ExecutionPlan
    states: NodeStateTable = field(default_factory=NodeStateTable)
    process_results: list[ProcessResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        counts: dict[NodeState, int] = {}
        for node in self.plan.nodes:
            status = self.states.status(node.id)
            counts[status] = counts.get(status, 0) + 1
        parts = [f"{count} {state.value}" for state, count in sorted(counts.items(), key=lambda i: i[0].value)]
        status = "BUILD OK" if self.success else "BUILD FAILED"
        return f"{status} ({', '.join(parts)})"


class NestableBuilder:
    """Marker for a builder another layer may legally wrap.

    ``AtomicBuilder`` is deliberately **not** a ``NestableBuilder``: its layer
    establishes the staging overlay and its ``build()`` carries the P0 staging
    hazard guard and the ``commit``/``discard`` workflow, so it must be the
    *outermost* builder. Because the wrapping builders type their ``inner`` as
    ``NestableBuilder``, a type checker (mypy/pyright, and so any IDE) flags
    ``CacheBuilder(AtomicBuilder(...))`` as it is typed; ``__init__`` also
    raises at runtime, since Python type hints are not enforced. The
    language-neutral form of this rule — atomic is the outermost layer in the
    wire spec — is validated in the Rust core, so every future frontend
    inherits it (see docs/boundary-schema.md)."""

    def _post_build(self) -> None:
        """Post-build maintenance, run once by the outermost `build()` and
        chained inward (cache eviction, ledger compaction). Only the outermost
        builder's `build()` executes — the engine runs the whole wrapped stack —
        so a wrapper must forward this to its `inner`. Default: forward if
        wrapping, else nothing."""
        inner = getattr(self, "inner", None)
        if inner is not None:
            inner._post_build()


def _reject_wrapped_atomic(inner: object) -> None:
    """Guard for a wrapping builder's ``inner``: an ``AtomicBuilder`` may never
    be wrapped (it must be outermost). Raises with a fix-it message."""
    if isinstance(inner, AtomicBuilder):
        raise TypeError(
            "AtomicBuilder must be the outermost builder — write "
            "AtomicBuilder(CacheBuilder(...)), not the reverse. Wrapping it "
            "buries its staging-hazard guard and commit/discard workflow."
        )


class CleanBuilder(NestableBuilder):
    """Builds everything the graph declares; no change detection."""

    def __init__(self, graph: Graph, root: str | Path = ".") -> None:
        self.graph = graph
        self.root = Path(root).resolve()

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return plan_graph(self.graph, self.root, targets)

    def layers(self) -> list:
        return []

    def source_graph(self) -> Graph:
        return self.graph

    def layer_specs(self) -> list[dict] | None:
        """This builder's layer stack as boundary-schema configs (innermost
        first), or None when it holds something that can't cross (a custom
        Python store object). Mirrors `layers()`."""
        return []

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None,
              worker_idle_timeout: int | None = None) -> BuildResult:
        """``worker_idle_timeout``: seconds a persistent worker's daemon
        stays warm between builds (default 900; 0 = no daemon). Rust engine
        only; the Python engine always terminates workers at build end."""
        from . import engine

        rust = engine.maybe_build(self, jobs=jobs, reporter=reporter, targets=targets,
                                  worker_idle_timeout=worker_idle_timeout)
        result = rust if rust is not None else self.plan(targets).run(
            jobs=jobs, layers=self.layers(), reporter=reporter
        )
        self._post_build()
        return result


class IncrementalBuilder(NestableBuilder):
    """Skips work whose fingerprints prove it up to date.

    ``graph_supplier`` is a callable so the builder decides when and how
    much graph to load (a plain ``Graph`` is also accepted). Metadata lives
    under ``metadata_dir``; if it is missing the build degrades to clean
    behavior and records everything for next time.
    """

    def __init__(
        self,
        graph_supplier,
        root: str | Path = ".",
        metadata_dir: str | Path = "build/.brix",
        detector=None,
        verify_contents: bool = False,
        scrub: bool = False,
        keep_builds: int | None = None,
    ) -> None:
        self._supplier = graph_supplier if callable(graph_supplier) else lambda: graph_supplier
        self.root = Path(root).resolve()
        self.metadata_dir = metadata_dir
        # Bound the metadata ledger (off by default — like the artifact cache,
        # it never discards unasked). When set, after each build a record for an
        # action/collection not present in the last ``keep_builds`` builds' graphs
        # is evicted (LRU by build). Safe: a dropped record only forces a
        # recompute next time. See `metadata.compact_metadata`.
        if keep_builds is not None and keep_builds < 1:
            raise ValueError("keep_builds must be >= 1 (or None to disable)")
        self._keep_builds = keep_builds
        # Scrub mode (opt-in): run each process with an *authoritative* environment
        # — only the plumbing base (brix.env.BASE_ENV) plus each action's declared
        # allowlist (SYSTEM/PASS names) and literal env=, minus UNSET — so an
        # undeclared ambient variable is simply absent and cannot silently affect
        # the build. Carried on the incremental layer (Python) and emitted as a
        # `scrub` wire flag (Rust). See docs/environment-recording.md.
        self._scrub = scrub
        # "Always re-check file contents": force the default detector to re-hash
        # every file instead of trusting its (mtime, size) stat cache, catching
        # the rare content change that leaves size and mtime unchanged. Opt-in
        # (slower); configures the default detector, and rides the boundary as a
        # `verify_contents` flag so the Rust engine's native detector honors it
        # too. When a custom `detector=` is supplied, that detector owns the
        # policy and this flag only crosses the wire.
        self._verify_contents = verify_contents
        # The change detector is an injected seam (one of brix's three: cache
        # store, content lens, change detector). Pass a factory `(root,
        # metadata) -> detector`; the builder owns the metadata lifecycle and
        # hands it in. `None` uses the default stat detector. `GitChangeDetector`
        # (brix.gitdetect) is a drop-in that accelerates via git — both existing
        # detectors already have the `(root, metadata)` shape, so you pass the
        # class. It accelerates *both* engines: the Python engine uses the class
        # directly; the Rust engine reads the `git_detection` flag `layer_specs`
        # emits (below) and runs its native mirror. A detector object can't
        # cross the boundary, so the flag is how the intent does.
        self._detector = detector

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return plan_graph(self._supplier(), self.root, targets)

    def layers(self) -> list:
        from .incremental import IncrementalLayer
        from .metadata import BuildMetadata, TimeAndHashChangeDetector

        metadata = BuildMetadata(self.root, self.metadata_dir)
        metadata.load()
        if self._detector is not None:
            detector = self._detector(self.root, metadata)
        else:
            detector = TimeAndHashChangeDetector(
                self.root, metadata, verify_contents=self._verify_contents
            )
        return [IncrementalLayer(metadata, detector, scrub=self._scrub)]

    def source_graph(self) -> Graph:
        return self._supplier()

    def layer_specs(self) -> list[dict] | None:
        spec = {"kind": "incremental", "metadata_dir": str(self.metadata_dir)}
        # Turn on the Rust-native git detector when a GitChangeDetector is
        # configured, so the default (Rust) engine gets the same acceleration
        # the Python engine already has (the detector object itself can't cross
        # the boundary — this flag is how the intent does).
        if getattr(self._detector, "wire_git_detection", False):
            spec["git_detection"] = True
        if self._verify_contents:
            spec["verify_contents"] = True
        if self._scrub:
            spec["scrub"] = True
        return [spec]

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None,
              worker_idle_timeout: int | None = None) -> BuildResult:
        """``worker_idle_timeout``: seconds a persistent worker's daemon
        stays warm between builds (default 900; 0 = no daemon). Rust engine
        only; the Python engine always terminates workers at build end."""
        from . import engine

        rust = engine.maybe_build(self, jobs=jobs, reporter=reporter, targets=targets,
                                  worker_idle_timeout=worker_idle_timeout)
        result = rust if rust is not None else self.plan(targets).run(
            jobs=jobs, layers=self.layers(), reporter=reporter
        )
        self._post_build()
        return result

    def _post_build(self) -> None:
        self._compact_if_bounded()

    def _compact_if_bounded(self) -> None:
        """After a build, bound the ledger if ``keep_builds`` was set. Stamps
        every action/collection the current graph realizes as seen this build
        (expanding ``ExpandAll`` through the memberships just recorded), then
        evicts records unseen for ``keep_builds`` builds. Runs in Python
        whichever engine built — the ledger is a shared JSON on disk.
        Best-effort: a graph-load or prune failure never fails the build."""
        if self._keep_builds is None:
            return
        from .metadata import compact_metadata

        try:
            graph = self._supplier()
        except Exception:
            return  # a graph that will not re-load post-build just skips compaction
        try:
            compact_metadata(
                self.root / self.metadata_dir / "metadata.json",
                keep_builds=self._keep_builds,
                graph=graph,
            )
        except OSError:
            pass


class CacheBuilder(NestableBuilder):
    """Reuse results across workspaces and clean checkouts.

    Wraps another builder; the cache layer sits innermost, keyed by the
    same ``action_fingerprint`` the incremental layer records.

    Backing store, pick one:

    - ``cache_dir`` (default) — the built-in local `CacheStore` under this
      directory. Absolute for a cache shared between checkouts, or
      workspace-relative; keep a relative one under a ``.brix``-prefixed
      directory (the default) so workspace scans ignore it.
    - ``store`` — any object with the `CacheStoreProtocol` three methods: a
      remote team cache, or a `TwoTierCacheStore` fronting one with local
      disk. An unreachable store degrades to a cache miss (handled in the
      layer), never a failed build.

    Passing both is an error.

    Eviction (built-in ``cache_dir`` store only) is **off by default** — a
    cache never discards without being asked. Set a bound to cap growth:

    - ``max_bytes`` — after each build, evict least-recently-*used* entries
      (a hit refreshes recency) until the store is under this size.
    - ``max_age`` — evict entries not used within this many seconds.

    Both may be set (TTL applies first, then the size cap). Eviction is safe
    by construction: every entry is reproducible, so a pruned entry becomes a
    recompute on its next build, never a wrong result. A custom ``store``
    manages its own retention, so bounds may not be combined with one.
    """

    def __init__(
        self,
        inner: NestableBuilder,
        cache_dir: str | Path | None = None,
        store: CacheStoreProtocol | None = None,
        max_bytes: int | None = None,
        max_age: float | None = None,
    ) -> None:
        if store is not None and cache_dir is not None:
            raise ValueError("CacheBuilder takes cache_dir or store, not both")
        if store is not None and (max_bytes is not None or max_age is not None):
            raise ValueError(
                "max_bytes/max_age apply to the built-in cache_dir store; a "
                "custom store manages its own retention"
            )
        _reject_wrapped_atomic(inner)
        self.inner = inner
        self._store = store
        self.cache_dir = (
            "build/.brix-cache" if (store is None and cache_dir is None) else cache_dir
        )
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.root: Path = inner.root

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return self.inner.plan(targets)

    def layers(self) -> list:
        from .cache import CacheLayer, CacheStore
        from .metadata import TimeAndHashChangeDetector

        inner_layers = self.inner.layers()
        # Share the incremental layer's detector when there is one, so no
        # file is hashed twice per build (fingerprints are the cache key).
        detector = next(
            (layer.detector for layer in inner_layers if hasattr(layer, "detector")),
            None,
        ) or TimeAndHashChangeDetector(self.root)
        store = self._store
        if store is None:
            directory = Path(self.cache_dir)
            if not directory.is_absolute():
                directory = self.root / directory
            store = CacheStore(directory)
        return [CacheLayer(store, detector), *inner_layers]

    def source_graph(self) -> Graph:
        return self.inner.source_graph()

    def layer_specs(self) -> list[dict] | None:
        inner = self.inner.layer_specs()
        if inner is None:
            return None
        if self._store is not None:
            # The live store object rides the spec; the engine boundary
            # lifts it out and sends it across as the cache_store extension
            # trait (docs/boundary-schema.md) — the core calls back into it.
            return [{"kind": "cache", "store": self._store}, *inner]
        return [{"kind": "cache", "cache_dir": str(self.cache_dir)}, *inner]

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None,
              worker_idle_timeout: int | None = None) -> BuildResult:
        """``worker_idle_timeout``: seconds a persistent worker's daemon
        stays warm between builds (default 900; 0 = no daemon). Rust engine
        only; the Python engine always terminates workers at build end."""
        from . import engine

        rust = engine.maybe_build(self, jobs=jobs, reporter=reporter, targets=targets,
                                  worker_idle_timeout=worker_idle_timeout)
        result = rust if rust is not None else self.plan(targets).run(
            jobs=jobs, layers=self.layers(), reporter=reporter
        )
        self._post_build()
        return result

    def _post_build(self) -> None:
        self._prune_if_bounded()
        self.inner._post_build()  # chain inward: e.g. ledger compaction

    def _prune_if_bounded(self) -> None:
        """After a build, cap the built-in store if a bound was set. Runs in
        Python whichever engine built — pruning is engine-agnostic directory
        maintenance over the shared cache dir (recency comes from the mtimes
        both engines refresh on a hit). Best-effort: a prune failure never
        fails the build."""
        if self._store is not None or (self.max_bytes is None and self.max_age is None):
            return
        from .cache import CacheStore

        directory = Path(self.cache_dir)
        if not directory.is_absolute():
            directory = self.root / directory
        try:
            CacheStore(directory).prune(max_bytes=self.max_bytes, max_age=self.max_age)
        except OSError:
            pass


class AtomicBuilder:
    """Either the whole build lands, or nothing on disk changes.

    Wraps another builder; its layer sits outermost, so process outputs
    and (later) cache-restored outputs all pass through staging. It is
    intentionally not a `NestableBuilder` — nothing may wrap it (see that
    class), so it is always the outermost builder.
    """

    def __init__(self, inner: NestableBuilder, staging_dir: str | Path = "build/staging") -> None:
        _reject_wrapped_atomic(inner)  # AtomicBuilder(AtomicBuilder(...)) is nonsense
        self.inner = inner
        self.staging_dir = staging_dir
        self.root: Path = inner.root

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return self.inner.plan(targets)

    def layers(self, hold: bool = False) -> list:
        from .atomic import AtomicLayer

        return [*self.inner.layers(), AtomicLayer(self.staging_dir, hold=hold)]

    def source_graph(self) -> Graph:
        return self.inner.source_graph()

    def layer_specs(self, hold: bool = False) -> list[dict] | None:
        inner = self.inner.layer_specs()
        if inner is None:
            return None
        return [*inner, {"kind": "atomic", "staging_dir": str(self.staging_dir), "hold": hold}]

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None, hold: bool = False,
              worker_idle_timeout: int | None = None) -> BuildResult:
        """``hold=True``: build fully but keep outputs in staging for
        review — the workspace changes only at `commit`.
        ``worker_idle_timeout``: see `IncrementalBuilder.build`."""
        from . import engine
        from .atomic import staging_hazards

        # P0 uniformity guard: refuse (loud, engine-agnostic, before any work) a
        # build whose tool self-resolves an intra-build artifact staging would
        # hide — never silently ship a stale result (docs/capability-audit.md).
        hazards = staging_hazards(self.source_graph())
        if hazards:
            raise ValueError(
                "atomic staging can't build an action whose tool resolves an "
                "intra-build artifact itself (a TypeScript import, a generated "
                "#include) — staging hides that artifact from the tool, which can "
                f"then silently read a stale one. Affected: {', '.join(hazards)}. "
                "Use the plain incremental stack (no AtomicBuilder) for this "
                "build, or compile the module whole-program in one step. See "
                "docs/capability-audit.md."
            )

        rust = engine.maybe_build(self, jobs=jobs, reporter=reporter, targets=targets, hold=hold,
                                  worker_idle_timeout=worker_idle_timeout)
        result = rust if rust is not None else self.plan(targets).run(
            jobs=jobs, layers=self.layers(hold=hold), reporter=reporter
        )
        self._post_build()
        return result

    def _post_build(self) -> None:
        self.inner._post_build()  # atomic adds no maintenance of its own; chain inward

    def commit(self) -> tuple[int, int] | None:
        """Apply held staging to the workspace. None if nothing is staged;
        (files, deletions) otherwise. Raises ValueError on failed-build
        staging, which must never reach the workspace."""
        from .atomic import commit_staging

        return commit_staging(self.root, self.root / self.staging_dir)

    def discard(self) -> int | None:
        """Throw staging away, workspace untouched. None if nothing is
        staged; the number of files discarded otherwise."""
        from .atomic import discard_staging

        return discard_staging(self.root / self.staging_dir)


# The concrete builder types, as a union — a return/parameter annotation that
# names every builder without a Protocol (which couldn't express the nominal
# "AtomicBuilder is not nestable" rule anyway).
Builder = "CleanBuilder | IncrementalBuilder | CacheBuilder | AtomicBuilder"


def build_stack(
    graph_supplier,
    root: str | Path = ".",
    *,
    metadata_dir: str | Path = "build/.brix",
    cache_dir: str | Path | None = "build/.brix-cache",
    store: "CacheStoreProtocol | None" = None,
    staging_dir: str | Path | None = "build/staging",
    detector=None,
    verify_contents: bool = False,
    scrub: bool = False,
) -> "AtomicBuilder | NestableBuilder":
    """Assemble the standard builder stack in the correct order —
    ``AtomicBuilder(CacheBuilder(IncrementalBuilder(...)))`` — so the ordering
    can't be gotten wrong (atomic outermost; see `NestableBuilder`).

    Layers toggle by argument: pass ``cache_dir=None`` (and no ``store``) to
    drop the cache, ``staging_dir=None`` to drop atomic staging (e.g. a
    cross-module compiler that self-resolves its classpath, which atomic
    staging can't support — see `examples/kotlin_multimodule`). ``detector`` is
    the change-detector factory threaded to the `IncrementalBuilder` (e.g.
    ``GitChangeDetector``); ``None`` uses the default stat detector.
    ``verify_contents=True`` makes the default detector re-hash every file
    instead of trusting its ``(mtime, size)`` cache — slower, but it catches a
    content change that leaves size and mtime unchanged. ``scrub=True`` runs each
    process with an *authoritative* environment (only the plumbing base plus each
    action's declared allowlist), so an undeclared ambient variable can't silently
    affect the build. Defaults give the full recommended stack.

    Returns a builder; for `cli.main`'s ``builder_factory`` (called fresh per
    invocation, so ``commit``/``discard`` rebuild cleanly), pass a thunk::

        cli.main(create_graph, builder_factory=lambda: build_stack(create_graph, root=ROOT))
    """
    inner: NestableBuilder = IncrementalBuilder(
        graph_supplier, root=root, metadata_dir=metadata_dir, detector=detector,
        verify_contents=verify_contents, scrub=scrub,
    )
    if store is not None:
        inner = CacheBuilder(inner, store=store)
    elif cache_dir is not None:
        inner = CacheBuilder(inner, cache_dir=cache_dir)
    if staging_dir is not None:
        return AtomicBuilder(inner, staging_dir=staging_dir)
    return inner
