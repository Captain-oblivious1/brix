"""The builder-stack ordering guard: AtomicBuilder must be the outermost builder.

Burying an AtomicBuilder inside another builder (e.g. `CacheBuilder(AtomicBuilder(
...))`) silently bypasses AtomicBuilder.build()'s P0 staging-hazard guard and
puts commit/discard out of reach, while — because layer specs self-order — still
producing a superficially working build. Two guards prevent it:

  * a nominal type hierarchy (`NestableBuilder`), so a type checker / IDE flags
    the bad nesting as written, plus a runtime `TypeError`;
  * language-neutral validation in the Rust core (the atomic layer must be the
    outermost/last spec entry), so every frontend inherits the rule.
"""

import pytest

from brix import AtomicBuilder, CacheBuilder, CleanBuilder, IncrementalBuilder
from brix.builders import NestableBuilder

from test_incremental import make_graph


# --- the nominal type hierarchy (what a type checker reads) -----------------

def test_wrappable_builders_are_nestable_atomic_is_not():
    assert issubclass(CleanBuilder, NestableBuilder)
    assert issubclass(IncrementalBuilder, NestableBuilder)
    assert issubclass(CacheBuilder, NestableBuilder)
    # The load-bearing exclusion: AtomicBuilder is deliberately NOT nestable,
    # which is what makes `inner: NestableBuilder` reject it in a type checker.
    assert not issubclass(AtomicBuilder, NestableBuilder)


# --- the runtime guard ------------------------------------------------------

def test_correct_nesting_is_accepted(tmp_path):
    builder = AtomicBuilder(CacheBuilder(IncrementalBuilder(make_graph, root=tmp_path)))
    assert isinstance(builder, AtomicBuilder)


def test_cache_cannot_wrap_atomic(tmp_path):
    with pytest.raises(TypeError, match="outermost"):
        CacheBuilder(AtomicBuilder(IncrementalBuilder(make_graph, root=tmp_path)))


def test_atomic_cannot_wrap_atomic(tmp_path):
    with pytest.raises(TypeError, match="outermost"):
        AtomicBuilder(AtomicBuilder(IncrementalBuilder(make_graph, root=tmp_path)))


def test_atomic_over_incremental_without_cache_is_fine(tmp_path):
    # No cache layer is a legal stack (IncrementalBuilder is nestable).
    builder = AtomicBuilder(IncrementalBuilder(make_graph, root=tmp_path))
    assert isinstance(builder, AtomicBuilder)


# --- the core backstop: a malformed spec is rejected at the boundary --------

@pytest.mark.parametrize("engine", ["rust"])
def test_core_rejects_atomic_not_outermost_in_spec(tmp_path, monkeypatch, engine):
    """The Python builders can't emit a bad-ordered spec, but a hand-built or
    future-frontend one could. The Rust core is the shared backstop: an atomic
    layer that isn't the outermost (last) spec entry is refused."""
    monkeypatch.setenv("BRIX_ENGINE", engine)
    from brix import engine as engine_mod
    from brix.lowering import graph_to_spec

    module = engine_mod._engine_for()
    if module is None:
        pytest.skip("rust engine unavailable")

    import json

    spec_json = json.dumps(graph_to_spec(make_graph()))
    # atomic first (innermost) — the inverted, invalid ordering.
    config = {
        "root": str(tmp_path),
        "jobs": None,
        "targets": None,
        "layers": [
            {"kind": "atomic", "staging_dir": "build/staging", "hold": False},
            {"kind": "incremental", "metadata_dir": "build/.brix"},
        ],
    }
    with pytest.raises(Exception, match="outermost"):
        module.build(spec_json, json.dumps(config))
