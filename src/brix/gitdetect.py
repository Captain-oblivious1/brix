"""Git-accelerated change detection (opt-in, reference implementation).

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
  baseline is the HEAD sha recorded at the previous build.
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


def _changed_since(repo_root: Path, baseline: str | None) -> set[str] | None:
    """Repo-relative paths changed since the baseline build: the working-tree
    diff (unstaged + staged + untracked) unioned with commits `baseline..HEAD`."""
    changed: set[str] = set()

    status = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all")
    if status is None:
        return None
    # Porcelain -z: each entry is "XY <path>", NUL-separated; a rename/copy adds
    # a second NUL-separated path (the origin). Parse defensively — any path
    # mentioned is a candidate, so both sides of a rename count as changed.
    tokens = [t for t in status.split("\0") if t]
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        code, path = entry[:2], entry[3:] if len(entry) > 3 else ""
        if path:
            changed.add(path)
        if code and (code[0] in ("R", "C")):
            i += 1  # the following token is the rename/copy source path
            if i < len(tokens):
                changed.add(tokens[i])
        i += 1

    if baseline:
        committed = _git(repo_root, "diff", "--name-only", "-z", f"{baseline}..HEAD")
        if committed is None:
            return None
        changed.update(p for p in committed.split("\0") if p)

    return changed


class GitChangeDetector:
    """Wraps a `TimeAndHashChangeDetector`, vouching tracked+unmodified files as
    unchanged without stat-ing them. Presents the same interface (the incremental
    and cache layers use it identically), sharing the inner detector's metadata.
    """

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
        self._ready = False
        self._prepare()

    def _prepare(self) -> None:
        repo_root = _repo_root(self.root)
        if repo_root is None:
            return  # not a git repo (or no git) -> pure fall-through
        baseline = self.metadata.git_baseline()
        tracked = _tracked(repo_root)
        changed = _changed_since(repo_root, baseline)
        head = _head(repo_root)
        # Record this build's HEAD as the next build's baseline (persisted with
        # the metadata at build end). HEAD is stable across a build.
        self.metadata.set_git_baseline(head)
        if tracked is None or changed is None or baseline is None:
            # No baseline yet (first git build / empty repo) or a git call
            # failed: vouch nothing this build, but the baseline is now set so
            # the *next* build can accelerate.
            return
        self._repo_root = repo_root
        self._tracked = tracked
        self._changed = changed
        self._ready = True

    def _vouch_unchanged(self, path: Path) -> bool:
        """True iff git guarantees this file's content equals the last build's
        (tracked, and not changed since the recorded baseline)."""
        if not self._ready:
            return False
        assert self._repo_root is not None and self._tracked is not None
        try:
            rel = path.resolve().relative_to(self._repo_root).as_posix()
        except ValueError:
            return False  # outside the repo -> the inner detector owns it
        return rel in self._tracked and rel not in self._changed

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
