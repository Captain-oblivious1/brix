"""The "always re-check file contents" mode.

To go fast, the default detector assumes a file is unchanged when its
``(mtime, size)`` both match the last build — so a content change that leaves
size and mtime identical (a same-size in-place edit that restores the mtime, or
a tool that rewrites a file preserving its timestamp) is missed. Opt-in
``verify_contents`` re-reads and re-hashes every file, trading speed for
certainty, and catches it.

These tests pin (a) that the fast path indeed misses such a collision — so the
mode is solving a real problem — and (b) that ``verify_contents`` catches it,
while an actually-unchanged file still does no work. Both engines run this file
(default Rust, and again under BRIX_ENGINE=py).
"""

import os
import shutil
from pathlib import Path

import pytest

from brix import ExternalProcessAction, Graph, IncrementalBuilder

needs_cp = pytest.mark.skipif(shutil.which("cp") is None, reason="no cp")

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


def _collide_edit(src: Path, text: str) -> None:
    """Rewrite src to new same-length content and restore the old mtime, so
    (mtime, size) are unchanged — the stat collision the mode targets."""
    src.write_text(text)
    os.utime(src, ns=(_MTIME_NS, _MTIME_NS))


@needs_cp
def test_fast_path_misses_a_same_stat_content_change(tmp_path):
    # Baseline: the *default* detector trusts (mtime, size) and misses this.
    src = tmp_path / "src.txt"
    _collide_edit(src, "aaaa\n")

    def builder():
        return IncrementalBuilder(_make_graph_supplier(), root=tmp_path)

    assert _ran(builder().build()) == ["copy"]
    assert (tmp_path / "out.txt").read_text() == "aaaa\n"

    _collide_edit(src, "bbbb\n")  # same size + mtime, different content
    second = builder().build()
    assert _ran(second) == [], "documents the fast-path blind spot"


@needs_cp
def test_verify_contents_catches_a_same_stat_content_change(tmp_path):
    src = tmp_path / "src.txt"
    _collide_edit(src, "aaaa\n")

    def builder():
        return IncrementalBuilder(
            _make_graph_supplier(), root=tmp_path, verify_contents=True
        )

    assert _ran(builder().build()) == ["copy"]
    assert (tmp_path / "out.txt").read_text() == "aaaa\n"

    _collide_edit(src, "bbbb\n")  # same (mtime, size), different content
    second = builder().build()
    assert _ran(second) == ["copy"], "verify_contents must re-read and catch it"
    assert (tmp_path / "out.txt").read_text() == "bbbb\n"


@needs_cp
def test_verify_contents_still_replays_an_unchanged_file(tmp_path):
    # The mode re-reads, but an actually-unchanged file yields the same digest,
    # so the dependent step still replays — no spurious rebuild.
    src = tmp_path / "src.txt"
    _collide_edit(src, "stable\n")

    def builder():
        return IncrementalBuilder(
            _make_graph_supplier(), root=tmp_path, verify_contents=True
        )

    assert _ran(builder().build()) == ["copy"]
    assert _ran(builder().build()) == []
    assert _ran(builder().build()) == []
