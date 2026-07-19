"""Symlinked inputs and the repoint hole.

A symlinked source is stat'd *through* to its target, so a change to the
target's content moves its (mtime, size) and is caught like any edit. The
subtle hole is a symlink **repointed** to a different file that happens to share
the old target's (mtime, size) — a release-style `current -> vN` swap with
mtimes preserved by `tar`/`rsync -a`. Without folding the link target into the
stat signature, the repoint collides and the build serves the old target's
cached hash — a stale output. These tests pin that both the content-change and
the repoint re-run the dependent step, and that a genuine no-op still replays.

Both engines run this file (default Rust, and again under BRIX_ENGINE=py), so
the fix is checked on the shipping engine and its Python mirror.
"""

import os
import shutil
from pathlib import Path

import pytest

from brix import ExternalProcessAction, Graph, IncrementalBuilder

needs_cp = pytest.mark.skipif(shutil.which("cp") is None, reason="no cp")

# A and B share a size; a caller forces them to share an mtime too, so the only
# thing distinguishing the two targets is the link itself.
_A = "content-A\n"
_B = "content-B\n"
_MTIME_NS = 1_600_000_000_000_000_000


def _make_graph_supplier():
    def create_graph():
        g = Graph()
        d = g.data
        g.add(
            ExternalProcessAction(
                name="copy",
                command_line="{cp} {src} {out}",
                inputs={"cp": d.tool("cp"), "src": d.file("src.txt")},
                outputs={"out": d.file("out.txt")},
            )
        )
        return g

    return create_graph


def _ran(result) -> list[str]:
    return [r.action_name for r in result.process_results if r.source == "process"]


@needs_cp
def test_symlink_repoint_with_colliding_stat_rebuilds(tmp_path):
    a = tmp_path / "A.txt"
    b = tmp_path / "B.txt"
    a.write_text(_A)
    b.write_text(_B)
    assert a.stat().st_size == b.stat().st_size  # same size on purpose
    os.utime(a, ns=(_MTIME_NS, _MTIME_NS))
    os.utime(b, ns=(_MTIME_NS, _MTIME_NS))  # ... and the same mtime

    src = tmp_path / "src.txt"
    src.symlink_to(a)

    def builder():
        return IncrementalBuilder(_make_graph_supplier(), root=tmp_path)

    # Cold build copies A's content through the link.
    first = builder().build()
    assert first.success
    assert _ran(first) == ["copy"]
    assert (tmp_path / "out.txt").read_text() == _A

    # Nothing changed: the step replays.
    second = builder().build()
    assert second.success
    assert _ran(second) == []

    # Repoint the link A -> B, with B carrying A's exact (mtime, size). Stat
    # through the link collides; only the folded-in target distinguishes them.
    src.unlink()
    src.symlink_to(b)
    os.utime(b, ns=(_MTIME_NS, _MTIME_NS))

    third = builder().build()
    assert third.success
    assert _ran(third) == ["copy"], "repoint to a colliding-stat target must rebuild"
    assert (tmp_path / "out.txt").read_text() == _B


@needs_cp
def test_symlink_target_content_change_rebuilds(tmp_path):
    # The ordinary case: same link, target's content changes (mtime moves).
    a = tmp_path / "A.txt"
    a.write_text(_A)
    src = tmp_path / "src.txt"
    src.symlink_to(a)

    def builder():
        return IncrementalBuilder(_make_graph_supplier(), root=tmp_path)

    first = builder().build()
    assert first.success and _ran(first) == ["copy"]

    assert _ran(builder().build()) == []  # replay

    a.write_text("content-A-edited\n")
    third = builder().build()
    assert _ran(third) == ["copy"]
    assert (tmp_path / "out.txt").read_text() == "content-A-edited\n"


@needs_cp
def test_symlink_stable_target_replays(tmp_path):
    # A symlink that neither repoints nor whose target changes must NOT rebuild
    # (guards against the fix over-invalidating on the symlink path).
    a = tmp_path / "A.txt"
    a.write_text(_A)
    os.utime(a, ns=(_MTIME_NS, _MTIME_NS))
    src = tmp_path / "src.txt"
    src.symlink_to(a)

    def builder():
        return IncrementalBuilder(_make_graph_supplier(), root=tmp_path)

    assert _ran(builder().build()) == ["copy"]
    assert _ran(builder().build()) == []
    assert _ran(builder().build()) == []
