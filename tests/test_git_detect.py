"""Git-accelerated change detection (opt-in reference path).

The load-bearing test is the **equivalence oracle**: an identical edit sequence,
replayed on two workspaces — one with `detector=GitChangeDetector`, one with the
plain stat detector — must produce byte-identical rebuild decisions at every step. The git
path is a pure performance layer, so if it ever vouches a file it shouldn't (or
fails to vouch one it should) the two run-sets diverge and this test fails.

Git-engine note: the git detector is a Python-engine layer today, so these run
under BRIX_ENGINE=py. Skips without git on PATH.
"""

import shutil
import subprocess

import pytest

from brix import IncrementalBuilder, Reporter
from brix.gitdetect import GitChangeDetector
from brix.metadata import BuildMetadata, TimeAndHashChangeDetector

from test_incremental import make_graph, write_workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text("build/\nruns.log\n__pycache__/\n")


def _commit(root, msg="c"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


class Ran(Reporter):
    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


def _sequence(root, detector, monkeypatch):
    """Run a fixed edit sequence; return the sorted run-set at each step."""
    monkeypatch.setenv("BRIX_ENGINE", "py")
    write_workspace(root, {"a": "x", "b": "y"})
    _init_repo(root)
    _commit(root)

    steps = []

    def build():
        rep = Ran()
        result = IncrementalBuilder(make_graph, root=root, detector=detector).build(reporter=rep)
        assert result.success, result.errors
        return sorted(rep.labels)

    steps.append(build())                                   # clean build
    steps.append(build())                                   # no-op
    (root / "src/a.txt").write_text("X")                    # edit a tracked source
    steps.append(build())
    (root / "src/c.txt").write_text("z")                    # add an untracked source
    steps.append(build())
    _commit(root)                                           # c becomes tracked
    steps.append(build())                                   # no-op after a commit
    (root / "src/c.txt").write_text("zz")                   # edit the now-tracked file
    steps.append(build())
    _commit(root)                                           # commit the c edit
    steps.append(build())                                   # no-op after the commit
    (root / "src/a.txt").write_text("A-DIRTY")              # dirty at build time
    steps.append(build())
    _git(root, "checkout", "--", "src/a.txt")               # revert to HEAD content
    steps.append(build())                                   # the revert IS a change
    steps.append(build())                                   # then clean again
    return steps


def test_git_and_stat_detectors_make_identical_decisions(tmp_path, monkeypatch):
    git_root = tmp_path / "git"
    stat_root = tmp_path / "stat"
    git_root.mkdir()
    stat_root.mkdir()

    git_steps = _sequence(git_root, detector=GitChangeDetector, monkeypatch=monkeypatch)
    stat_steps = _sequence(stat_root, detector=None, monkeypatch=monkeypatch)

    assert git_steps == stat_steps


def test_no_op_build_is_clean_under_git(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "py")
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    _init_repo(tmp_path)
    _commit(tmp_path)

    assert IncrementalBuilder(make_graph, root=tmp_path, detector=GitChangeDetector).build().success
    rep = Ran()  # second build: nothing should run
    assert IncrementalBuilder(make_graph, root=tmp_path, detector=GitChangeDetector).build(reporter=rep).success
    assert rep.labels == []


# --- targeted vouch logic ---------------------------------------------------

def _detector(root):
    md = BuildMetadata(root, "build/.brix")
    md.load()
    return GitChangeDetector(root, inner=TimeAndHashChangeDetector(root, md)), md


def test_first_build_has_no_baseline_so_vouches_nothing(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    _init_repo(tmp_path)
    _commit(tmp_path)
    det, _ = _detector(tmp_path)
    # No baseline recorded yet -> not ready -> nothing vouched.
    assert det._vouch_unchanged(tmp_path / "src/a.txt") is False


def test_unchanged_tracked_file_is_vouched_after_baseline(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    _init_repo(tmp_path)
    _commit(tmp_path)
    # First detector records the baseline (this build's HEAD).
    det1, md = _detector(tmp_path)
    md.save()
    # Second detector, same HEAD, unchanged tracked file -> vouched.
    det2, _ = _detector(tmp_path)
    assert det2._vouch_unchanged(tmp_path / "src/a.txt") is True
    # An edited tracked file is in the working-tree diff -> not vouched.
    (tmp_path / "src/a.txt").write_text("CHANGED")
    det3, _ = _detector(tmp_path)
    assert det3._vouch_unchanged(tmp_path / "src/a.txt") is False


def test_untracked_and_out_of_tree_files_are_never_vouched(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    _init_repo(tmp_path)
    _commit(tmp_path)
    det1, md = _detector(tmp_path)
    md.save()
    (tmp_path / "src/untracked.txt").write_text("u")
    det2, _ = _detector(tmp_path)
    # Untracked: git can't vouch content -> falls through to stat/hash.
    assert det2._vouch_unchanged(tmp_path / "src/untracked.txt") is False
    # Outside the repo entirely -> the inner detector owns it.
    assert det2._vouch_unchanged(tmp_path.parent / "elsewhere.txt") is False


def test_file_dirty_at_last_build_is_not_vouched_after_revert(tmp_path):
    # The revert hole: a file dirty at build N (its dirty content's hash is what
    # the metadata stores), reverted to HEAD content before build N+1. Git then
    # reports it clean and baseline..HEAD is empty — but its content is NOT what
    # the last build hashed, so vouching it would replay a stale hash. The
    # previous build's persisted dirty set must block the vouch.
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    _init_repo(tmp_path)
    _commit(tmp_path)
    det1, md1 = _detector(tmp_path)  # records baseline + (empty) dirty set
    md1.save()
    (tmp_path / "src/a.txt").write_text("DIRTY")
    det2, md2 = _detector(tmp_path)
    assert det2._vouch_unchanged(tmp_path / "src/a.txt") is False  # dirty now
    md2.save()  # persists {src/a.txt} as this build's dirty set
    _git(tmp_path, "checkout", "--", "src/a.txt")  # revert: clean vs HEAD again
    det3, _ = _detector(tmp_path)
    assert det3._vouch_unchanged(tmp_path / "src/a.txt") is False  # was dirty last build
    assert det3._vouch_unchanged(tmp_path / "src/b.txt") is True   # untouched: still vouched


def test_changed_paths_enumerates_since_last_build(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    _init_repo(tmp_path)
    _commit(tmp_path)
    det1, md1 = _detector(tmp_path)
    assert det1.changed_paths() is None  # no baseline yet -> caller loads all
    md1.save()
    (tmp_path / "src/a.txt").write_text("edited")
    det2, md2 = _detector(tmp_path)
    assert det2.changed_paths() == {(tmp_path / "src/a.txt").resolve()}
    md2.save()
    _git(tmp_path, "checkout", "--", "src/a.txt")  # revert before the next build
    det3, _ = _detector(tmp_path)
    # Clean vs HEAD now, but changed *since the last build* — the previous
    # build's dirty set carries the reverted file into the enumeration.
    assert det3.changed_paths() == {(tmp_path / "src/a.txt").resolve()}


def test_not_a_git_repo_degrades_to_inner(tmp_path):
    write_workspace(tmp_path, {"a": "x"})  # no git init
    det, _ = _detector(tmp_path)
    assert det._vouch_unchanged(tmp_path / "src/a.txt") is False
    # And hash_file still works, delegating to the stat detector.
    assert det.hash_file(tmp_path / "src/a.txt") is not None
