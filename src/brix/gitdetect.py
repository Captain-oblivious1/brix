"""Git-accelerated change detection (opt-in; mirrored natively in the Rust core).

The default `TimeAndHashChangeDetector` finds what changed by ``stat``-ing every
declared input each build. On a large working tree those stats become the floor.
This detector shortcuts the common case: for a **tracked** file that git reports
unchanged since the last build, it trusts the stored hash *without stat-ing it*.
Everything git can't vouch for — untracked files, ignored files (including the
build's own generated intermediates), and inputs outside the repo — falls
through to the wrapped detector unchanged.

It is a **pure performance layer**: it must never change a fingerprint. Its one
correctness obligation is the vouch — "tracked and unmodified since the recorded
baseline ⇒ content equals the last build's, so the cached hash is valid" — and
`tests/test_git_detect.py` proves it by equivalence against the stat detector.

Design notes (see docs/roadmap.md, "use git to spot changes instantly"):

* **git is the authority, never a reimplemented `.gitignore`.** The vouchable
  set is exactly git's *tracked* set (`git ls-files`); untracked and ignored
  files are never vouched, so brix stat/hashes them — but only the few that are
  actually declared inputs, so there is nothing to hunt for.
* **"Since last build", not "since last commit", and no forced commits.** The
  changed set unions the working-tree diff with `git diff <baseline>..HEAD`, so
  commits *between* builds are caught and the user need never commit. The
  baseline is the HEAD sha recorded at the previous build, **paired with that
  build's working-tree dirty set**: a file dirty at the last build was hashed
  with content git never committed, so if it is reverted before this build git
  reports it clean while the cached hash is stale — such files are never
  vouched (and count as changed in `changed_paths()`).
* **Degrades to the wrapped detector** whenever the workspace is not a git repo,
  git is unavailable, there is no baseline yet (first build, or an empty repo
  with no commits), or any git call fails — the safe direction is to fall back
  to the full scan, never to over-vouch.
* Residual risk equals the wrapped detector's: git's own status uses stat
  shortcuts in the index, so a content change preserving mtime+size can be
  missed by *both* — the "always re-hash" mode is the paranoid answer, not this.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .metadata import BuildMetadata, TimeAndHashChangeDetector


def _git(root: Path, *args: str) -> str | None:
    """Run a git command under ``root``; None on any failure (missing git, not
    a repo, nonzero exit) so every caller degrades to the stat scan."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _repo_root(root: Path) -> Path | None:
    out = _git(root, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if out else None


def _head(root: Path) -> str | None:
    out = _git(root, "rev-parse", "HEAD")
    return out.strip() if out else None


def _tracked(repo_root: Path) -> set[str] | None:
    """Repo-relative POSIX paths of every tracked file (`git ls-files`)."""
    out = _git(repo_root, "ls-files", "-z")
    if out is None:
        return None
    return {p for p in out.split("\0") if p}


def _worktree_dirty(repo_root: Path) -> set[str] | None:
    """Repo-relative paths dirty in the working tree right now (unstaged +
    staged + untracked), from `git status --porcelain`."""
    status = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all")
    if status is None:
        return None
    dirty: set[str] = set()
    # Porcelain -z: each entry is "XY <path>", NUL-separated; a rename/copy adds
    # a second NUL-separated path (the origin). Parse defensively — any path
    # mentioned is a candidate, so both sides of a rename count as changed.
    tokens = [t for t in status.split("\0") if t]
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        code, path = entry[:2], entry[3:] if len(entry) > 3 else ""
        if path:
            dirty.add(path)
        if code and (code[0] in ("R", "C")):
            i += 1  # the following token is the rename/copy source path
            if i < len(tokens):
                dirty.add(tokens[i])
        i += 1
    return dirty


def _committed_since(repo_root: Path, baseline: str) -> set[str] | None:
    """Repo-relative paths touched by commits in `baseline..HEAD`."""
    out = _git(repo_root, "diff", "--name-only", "-z", f"{baseline}..HEAD")
    if out is None:
        return None
    return {p for p in out.split("\0") if p}


class GitChangeDetector:
    """Wraps a `TimeAndHashChangeDetector`, vouching tracked+unmodified files as
    unchanged without stat-ing them. Presents the same interface (the incremental
    and cache layers use it identically), sharing the inner detector's metadata.

    Configuring this detector accelerates *both* engines: the Python engine uses
    this class directly, and on the Rust engine the builder emits a
    ``git_detection`` flag (see `builders.py`) that turns on the native mirror
    in `rust/brix-core/src/gitdetect.rs` — same vouch, same baseline/dirty
    metadata, so a build started on one engine continues correctly on the other.
    """

    # Marker read by IncrementalBuilder.layer_specs to turn on the Rust-native
    # git detector across the boundary. A custom detector factory without this
    # attribute simply doesn't accelerate the Rust engine (degrades to stat).
    wire_git_detection = True

    def __init__(
        self,
        root: Path | str = ".",
        metadata: BuildMetadata | None = None,
        inner: TimeAndHashChangeDetector | None = None,
    ) -> None:
        self.root = Path(root)
        self.inner = inner if inner is not None else TimeAndHashChangeDetector(self.root, metadata)
        self.metadata = self.inner.metadata
        self._repo_root: Path | None = None
        self._tracked: set[str] | None = None
        self._changed: set[str] | None = None
        self._previous_dirty: set[str] | None = None
        self._ready = False
        self._prepare()

    def _prepare(self) -> None:
        repo_root = _repo_root(self.root)
        if repo_root is None:
            return  # not a git repo (or no git) -> pure fall-through
        baseline = self.metadata.git_baseline()
        previous_dirty = self.metadata.git_dirty()
        tracked = _tracked(repo_root)
        dirty_now = _worktree_dirty(repo_root)
        head = _head(repo_root)
        # Record this build's (HEAD, working-tree dirty set) as the next
        # build's baseline pair (persisted with the metadata at build end).
        # They must be recorded together: the dirty set is what makes a later
        # "clean vs HEAD" answer trustworthy — a file dirty *now* is hashed
        # with its dirty content, so if it is reverted before the next build,
        # git will call it clean while the cached hash is stale. If either
        # half is unknowable, clear both so the next build vouches nothing.
        if head is not None and dirty_now is not None:
            self.metadata.set_git_baseline(head)
            self.metadata.set_git_dirty(sorted(dirty_now))
        else:
            self.metadata.set_git_baseline(None)
            self.metadata.set_git_dirty(None)
            return
        if baseline is None or previous_dirty is None or tracked is None:
            # No baseline/dirty pair yet (first git build, or metadata written
            # before the dirty set existed) or ls-files failed: vouch nothing
            # this build, but the pair is now set so the *next* build can
            # accelerate.
            return
        committed = _committed_since(repo_root, baseline)
        if committed is None:
            return  # baseline no longer resolvable (rebase/gc) -> full scan
        self._repo_root = repo_root
        self._tracked = tracked
        self._changed = dirty_now | committed
        self._previous_dirty = set(previous_dirty)
        self._ready = True

    def _vouch_unchanged(self, path: Path) -> bool:
        """True iff git guarantees this file's content equals what the last
        build hashed: tracked, not changed since the recorded baseline, and
        not dirty *at* the last build (a then-dirty file was hashed with
        content git never saw — reverting it makes git report clean while the
        cached hash is stale)."""
        if not self._ready:
            return False
        assert self._repo_root is not None and self._tracked is not None
        assert self._previous_dirty is not None
        try:
            rel = path.resolve().relative_to(self._repo_root).as_posix()
        except ValueError:
            return False  # outside the repo -> the inner detector owns it
        return (
            rel in self._tracked
            and rel not in self._changed
            and rel not in self._previous_dirty
        )

    def changed_paths(self) -> set[Path] | None:
        """Absolute paths changed since the last build — the working-tree diff,
        commits since the baseline, and the last build's dirty set (a file
        dirty then and clean now was *reverted*, which is still a change since
        that build). None when git cannot answer (no repo, no baseline, a git
        call failed): callers must treat None as "anything may have changed".

        This is the enumeration query the lazy-loading tier uses to decide
        which packages to load, independent of any loaded graph."""
        if not self._ready:
            return None
        assert self._repo_root is not None
        assert self._changed is not None and self._previous_dirty is not None
        return {self._repo_root / rel for rel in (self._changed | self._previous_dirty)}

    # -- detector interface (delegates, with the vouch fast path) ----------

    def stat_signature(self, path: Path):
        return self.inner.stat_signature(path)

    def hash_file(self, path: Path) -> str | None:
        if self._vouch_unchanged(path):
            cached = self.metadata.hash_by_path(str(path))
            if cached is not None:
                return cached  # unchanged since last build; no stat, no re-hash
        return self.inner.hash_file(path)

    def identity_digest(self, path: Path, identity) -> str | None:
        if self._vouch_unchanged(path):
            from .identity import tag as identity_tag

            cached = self.metadata.identity_by_path(str(path), identity_tag(identity))
            if cached is not None:
                return cached
        return self.inner.identity_digest(path, identity)
