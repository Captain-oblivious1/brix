"""CLI helpers: the command-line section of a project's brix.py.

Brix is a library; the CLI is a thin client over builders, status, and
metadata. A project's build file ends with:

    if __name__ == "__main__":
        sys.exit(brix.cli.main(create_graph))

Commands (default: build):

    brix.py [build] [-j N] [--silent | --quiet-replays] [--stage]
    brix.py status      exit 0 = everything fresh, 1 = work pending
    brix.py errors      replay last recorded failures; exit 1 if any
    brix.py commit      apply staging held by `build --stage`
    brix.py discard     throw held/failed staging away
    brix.py clean       delete built outputs and metadata
    brix.py graph       print the execution plan's structure
    brix.py help        show this build file's live configuration
    brix.py env         show the last build's environment and what changed

`env` reports the environment the most recent build ran under and diffs it
against the current one (exit 1 if it differs) — the "works on my machine"
detective; a build captures it as a side effect (docs/environment-recording.md).
`help` introspects the builder chain (which layers are wired, so whether
`--stage` / `commit` / `discard` apply), the target aliases, and the
project's `commands=`, so the effect of `build` is visible without reading
the file. `build --stage` builds fully but holds outputs in staging for review —
the workspace changes only at `commit`. Requires an atomic builder.

Projects can add build-then-act subcommands via ``commands=`` — each
builds its targets first, then hands the built paths to a project
function (``brix.py view wealth`` → build wealth's video, then play it).

The engine never prints; this module is a client, so printing here is
fine — it is the client.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import environment
from .builders import AtomicBuilder, IncrementalBuilder
from .data import FileData, GlobData
from .metadata import BuildMetadata
from .plan import NodeKind
from .planner import plan_graph
from .report import ConsoleReporter
from .status import compute_status


# Public API surface (curated for documentation and as a contract).
__all__ = ['main', 'Alias']


@dataclass(frozen=True)
class Alias:
    """A target shorthand that also carries a description for ``help``.

    Aliases may be given as plain values (``{"app": "app/dist/main.js"}``)
    or, when a one-line explanation is worth surfacing, as
    ``{"app": cli.Alias("app/dist/main.js", "the bundled web app")}``. The
    ``target`` is a single output path or a list of them, exactly as a bare
    alias value; only ``help`` reads ``description``.
    """

    target: str | list[str]
    description: str = ""


def main(
    graph_supplier,
    root: str | Path = ".",
    argv: list[str] | None = None,
    metadata_dir: str | Path = "build/.brix",
    staging_dir: str | Path = "build/staging",
    builder_factory=None,
    aliases: dict[str, str | list[str]] | None = None,
    commands: dict[str, Callable[[list[Path]], int | None]] | None = None,
    env_ignore: list[str] | None = None,
) -> int:
    """``builder_factory`` (optional): zero-argument callable returning the
    builder to use for ``build`` — e.g. the full
    ``AtomicBuilder(IncrementalBuilder(...))`` stack. Defaults to a plain
    ``IncrementalBuilder``.

    ``aliases`` (optional): target shorthands — ``{"wealth":
    "output/videos/wealth.mpg"}`` lets users say ``build wealth``.

    ``commands`` (optional): build-then-act subcommands — ``{"view": fn}``
    adds ``brix.py view <target>...``, which builds the targets (aliases
    apply) and, on success, calls ``fn(paths)`` with their absolute
    ``Path``s. ``fn`` may return an exit code.

    ``env_ignore`` (optional): extra environment-variable names/globs to drop
    from the recorded build environment, on top of the built-in ignore-list of
    session noise (see ``brix.environment`` and ``brix.py env``)."""
    parser = argparse.ArgumentParser(prog="brix.py")
    subparsers = parser.add_subparsers(dest="command")

    def add_build_options(sub, targets_nargs: str) -> None:
        sub.add_argument("targets", nargs=targets_nargs, help="output paths or aliases")
        sub.add_argument("-j", "--jobs", type=int, default=None)
        quietness = sub.add_mutually_exclusive_group()
        quietness.add_argument("--silent", action="store_true", help="no output; exit code only")
        quietness.add_argument(
            "--quiet-replays", action="store_true", help="print only real work"
        )
        daemon = sub.add_mutually_exclusive_group()
        daemon.add_argument(
            "--no-daemon", action="store_true",
            help="don't keep persistent workers warm between builds",
        )
        daemon.add_argument(
            "--worker-timeout", type=int, default=None, metavar="SECS",
            help="seconds a warm worker survives between builds (default 900)",
        )

    build_parser = subparsers.add_parser("build", help="build (the default)")
    add_build_options(build_parser, "*")
    build_parser.add_argument(
        "--stage", action="store_true",
        help="hold outputs in staging for review; `commit` applies them",
    )
    status_parser = subparsers.add_parser("status", help="what would build, and why")
    status_parser.add_argument("targets", nargs="*")
    subparsers.add_parser("errors", help="show last recorded failures")
    subparsers.add_parser("commit", help="apply staging held by `build --stage`")
    subparsers.add_parser("discard", help="throw held/failed staging away")
    subparsers.add_parser("clean", help="delete built outputs and metadata")
    subparsers.add_parser("graph", help="print the execution plan structure")
    subparsers.add_parser("help", help="show this build file's live configuration")
    subparsers.add_parser(
        "env", help="show the last build's environment and what changed since"
    )
    for name in commands or {}:
        add_build_options(
            subparsers.add_parser(name, help=f"build target(s), then {name} them"), "+"
        )

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        argv = ["build"]  # default command, with build's own defaults
    args = parser.parse_args(argv)
    command = args.command
    root = Path(root).resolve()

    def resolve_targets() -> list[str] | None:
        resolved: list[str] = []
        for target in getattr(args, "targets", []):
            expansion = (aliases or {}).get(target, target)
            if isinstance(expansion, Alias):
                expansion = expansion.target
            resolved.extend([expansion] if isinstance(expansion, str) else expansion)
        return resolved or None

    if command == "build" or command in (commands or {}):
        if args.silent:
            reporter = None
        else:
            reporter = ConsoleReporter(show_replays=not args.quiet_replays)
        if builder_factory is not None:
            builder = builder_factory()
        else:
            builder = IncrementalBuilder(graph_supplier, root=root, metadata_dir=metadata_dir)
        kwargs = {}
        if getattr(args, "stage", False):
            if not isinstance(builder, AtomicBuilder):
                print("brix: --stage requires an AtomicBuilder (outermost)", file=sys.stderr)
                return 2
            kwargs["hold"] = True
        if args.no_daemon:
            kwargs["worker_idle_timeout"] = 0
        elif args.worker_timeout is not None:
            kwargs["worker_idle_timeout"] = args.worker_timeout
        try:
            targets = resolve_targets()
            result = builder.build(jobs=args.jobs, reporter=reporter, targets=targets, **kwargs)
        except ValueError as exc:
            print(f"brix: {exc}", file=sys.stderr)
            return 2
        # Record the environment this build ran under (success or failure — a
        # failed build's environment is equally worth diffing). Diagnostic
        # state only: it never rebuilds, fingerprints, or splits a cache.
        environment.write_record(
            root / metadata_dir,
            command=command,
            variables=environment.capture(ignore=env_ignore),
        )
        if not result.success:
            return 1
        if command == "build":
            return 0
        code = commands[command]([root / t for t in targets])
        return int(code or 0)

    if command == "status":
        try:
            report = compute_status(
                graph_supplier,
                root=root,
                metadata_dir=metadata_dir,
                staging_dir=staging_dir,
                targets=resolve_targets(),
            )
        except ValueError as exc:
            print(f"brix: {exc}", file=sys.stderr)
            return 2
        print(report.render())
        # A build-wide heads-up, independent of the graph's freshness: the
        # environment differs from the last recorded build. Diagnostic only —
        # it does not change status's clean/pending exit code.
        note = environment.summary_line(
            root / metadata_dir, environment.capture(ignore=env_ignore)
        )
        if note is not None:
            print(note)
        return 0 if report.is_clean else 1

    if command == "errors":
        report = compute_status(graph_supplier, root=root, metadata_dir=metadata_dir)
        failures = report.failures
        for status in failures:
            result = status.last_result
            print(result.summary() if result else f"FAILED: {status.label}")
            if result:
                for text in (result.stdout, result.stderr):
                    if text:
                        print(text.rstrip("\n"))
        if not failures:
            print("no recorded failures")
        return 1 if failures else 0

    if command in ("commit", "discard"):
        builder = builder_factory() if builder_factory is not None else None
        if not isinstance(builder, AtomicBuilder):
            print(f"brix: {command} requires an AtomicBuilder (outermost)", file=sys.stderr)
            return 2
        try:
            outcome = builder.commit() if command == "commit" else builder.discard()
        except ValueError as exc:
            print(f"brix: {exc}", file=sys.stderr)
            return 2
        if outcome is None:
            print("nothing staged")
        elif command == "commit":
            files, deletions = outcome
            print(f"committed {files} file(s), {deletions} deletion(s)")
        else:
            print(f"discarded {outcome} staged file(s)")
        return 0

    if command == "clean":
        removed = _clean(graph_supplier, root, metadata_dir)
        print(f"removed {removed} file(s)")
        return 0

    if command == "graph":
        graph = graph_supplier() if callable(graph_supplier) else graph_supplier
        print(plan_graph(graph, root).describe())
        return 0

    if command == "help":
        if builder_factory is not None:
            builder = builder_factory()
        else:
            builder = IncrementalBuilder(graph_supplier, root=root, metadata_dir=metadata_dir)
        print(_render_help(builder, aliases, commands))
        return 0

    if command == "env":
        text, changed = environment.render(
            root / metadata_dir, environment.capture(ignore=env_ignore)
        )
        print(text)
        return 1 if changed else 0

    parser.error(f"unknown command {command!r}")
    return 2


def _describe_stack(builder) -> list[tuple[str, str]]:
    """The active builder chain as ``(layer, detail)`` rows, outermost
    first. Reads wrapper attributes only — it never instantiates the layers
    or touches disk, so it is safe to call for a pure printout."""
    rows: list[tuple[str, str]] = []
    current = builder
    for _ in range(16):  # guard a malformed/cyclic chain
        if current is None:
            break
        cls = type(current).__name__
        if cls == "AtomicBuilder":
            rows.append(("Atomic", f"all-or-nothing; staging → {current.staging_dir}"))
        elif cls == "CacheBuilder":
            where = (
                "custom store"
                if getattr(current, "_store", None) is not None
                else f"cache → {current.cache_dir}"
            )
            rows.append(("Cache", f"reuse across checkouts; {where}"))
        elif cls == "IncrementalBuilder":
            rows.append(("Incremental", f"skip unchanged work; metadata → {current.metadata_dir}"))
        elif cls == "CleanBuilder":
            rows.append(("Clean", "rebuild everything; no change detection"))
        else:
            rows.append((cls, ""))
        current = getattr(current, "inner", None)
    return rows


def _alias_rows(aliases) -> list[tuple[str, str, str]]:
    """``(name, target, description)`` for each alias; a list target is
    space-joined, and a bare (non-:class:`Alias`) value has no description."""
    rows: list[tuple[str, str, str]] = []
    for name, value in (aliases or {}).items():
        if isinstance(value, Alias):
            target, description = value.target, value.description
        else:
            target, description = value, ""
        if not isinstance(target, str):
            target = " ".join(target)
        rows.append((name, target, description))
    return rows


def _doc_first_line(fn) -> str:
    for line in (fn.__doc__ or "").strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _render_help(builder, aliases, commands) -> str:
    """The ``help`` printout: the live builder stack, the project's target
    aliases and build-then-act commands, and what `build` will therefore do
    — everything that lives in the ``cli.main(...)`` call and is otherwise
    invisible without reading ``brix.py``."""
    stack = _describe_stack(builder)
    has_atomic = any(name == "Atomic" for name, _ in stack)
    lines: list[str] = []

    lines.append("Build stack (outermost first):")
    width = max((len(name) for name, _ in stack), default=0)
    for name, detail in stack:
        lines.append(f"  {name.ljust(width)}  {detail}".rstrip())

    lines.append("")
    lines.append("Commands:")
    fixed = [
        ("build [targets]", "build the requested targets, or everything (the default)"),
        ("status [targets]", "what would build, and why; exit 1 if work is pending"),
        ("errors", "replay the last recorded failures"),
        ("commit", "apply staging held by `build --stage`"),
        ("discard", "throw held or failed staging away"),
        ("clean", "delete built outputs and metadata"),
        ("graph", "print the execution plan structure"),
        ("help", "show this configuration"),
        ("env", "show the last build's environment and what changed"),
    ]
    command_rows = list(fixed)
    for name in commands or {}:
        doc = _doc_first_line(commands[name])
        summary = doc or f"build target(s), then {name} them"
        command_rows.append((f"{name} <targets>", summary))
    width = max(len(name) for name, _ in command_rows)
    staging_only = {"commit", "discard"}
    for name, summary in command_rows:
        note = ""
        if name in staging_only and not has_atomic:
            note = "  (needs an AtomicBuilder — not wired here)"
        lines.append(f"  {name.ljust(width)}  {summary}{note}")

    stage_note = (
        "`build --stage` and `commit`/`discard` are available (staging is wired)."
        if has_atomic
        else "`build --stage`, `commit`, and `discard` need an AtomicBuilder (not wired here)."
    )
    lines.append("")
    lines.append(stage_note)

    alias_rows = _alias_rows(aliases)
    if alias_rows:
        lines.append("")
        lines.append("Targets (aliases):")
        width = max(len(name) for name, _, _ in alias_rows)
        for name, target, description in alias_rows:
            suffix = f"   {description}" if description else ""
            lines.append(f"  {name.ljust(width)}  → {target}{suffix}")

    return "\n".join(lines)


def _clean(graph_supplier, root: Path, metadata_dir: str | Path) -> int:
    """Delete declared outputs (concrete and currently realized), every
    output recorded in metadata, and the metadata store itself."""
    graph = graph_supplier() if callable(graph_supplier) else graph_supplier

    from . import engine

    removed = engine.maybe_clean(graph, root, metadata_dir)
    if removed is not None:
        return removed

    plan = plan_graph(graph, root)
    metadata = BuildMetadata(root, metadata_dir)
    metadata.load()

    paths: set[str] = set()
    for node in plan.nodes:
        actions = [node.action.child] if node.kind is NodeKind.EXPAND else [node.action]
        for action in actions:
            for expr in getattr(action, "outputs", {}).values():
                for atom in expr.atoms():
                    if isinstance(atom, FileData) and atom.pattern.is_concrete:
                        paths.add(atom.path)
                    elif isinstance(atom, GlobData):
                        paths.update(p for p, _ in atom.pattern.find(root))
    for record in metadata.action_records().values():
        paths.update(record.get("outputs", {}))

    removed = 0
    for rel in sorted(paths):
        target = root / rel
        if target.is_file():
            target.unlink()
            removed += 1
    if metadata.directory.exists():
        shutil.rmtree(metadata.directory)
    return removed
