"""CLI helpers: the command-line section of a project's brix.py.

Brix is a library; the CLI is a thin client over builders, status, and
metadata. A project's build file ends with:

    if __name__ == "__main__":
        sys.exit(brix.cli.main(create_graph))

Commands (default: build):

    brix.py [build] [-j N] [--silent | --quiet-replays]
    brix.py status      exit 0 = everything fresh, 1 = work pending
    brix.py errors      replay last recorded failures; exit 1 if any
    brix.py clean       delete built outputs and metadata
    brix.py graph       print the execution plan's structure

The engine never prints; this module is a client, so printing here is
fine — it is the client.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .builders import IncrementalBuilder
from .data import FileData, GlobData
from .metadata import BuildMetadata
from .plan import NodeKind
from .planner import plan_graph
from .report import ConsoleReporter
from .status import compute_status


def main(
    graph_supplier,
    root: str | Path = ".",
    argv: list[str] | None = None,
    metadata_dir: str | Path = "build/.brix",
    staging_dir: str | Path = "build/staging",
) -> int:
    parser = argparse.ArgumentParser(prog="brix.py")
    commands = parser.add_subparsers(dest="command")

    build_parser = commands.add_parser("build", help="build (the default)")
    build_parser.add_argument("-j", "--jobs", type=int, default=None)
    quietness = build_parser.add_mutually_exclusive_group()
    quietness.add_argument("--silent", action="store_true", help="no output; exit code only")
    quietness.add_argument(
        "--quiet-replays", action="store_true", help="print only real work"
    )
    commands.add_parser("status", help="what would build, and why")
    commands.add_parser("errors", help="show last recorded failures")
    commands.add_parser("clean", help="delete built outputs and metadata")
    commands.add_parser("graph", help="print the execution plan structure")

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        argv = ["build"]  # default command, with build's own defaults
    args = parser.parse_args(argv)
    command = args.command
    root = Path(root).resolve()

    if command == "build":
        if args.silent:
            reporter = None
        else:
            reporter = ConsoleReporter(show_replays=not args.quiet_replays)
        builder = IncrementalBuilder(graph_supplier, root=root, metadata_dir=metadata_dir)
        result = builder.build(jobs=args.jobs, reporter=reporter)
        return 0 if result.success else 1

    if command == "status":
        report = compute_status(
            graph_supplier, root=root, metadata_dir=metadata_dir, staging_dir=staging_dir
        )
        print(report.render())
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

    if command == "clean":
        removed = _clean(graph_supplier, root, metadata_dir)
        print(f"removed {removed} file(s)")
        return 0

    if command == "graph":
        graph = graph_supplier() if callable(graph_supplier) else graph_supplier
        print(plan_graph(graph, root).describe())
        return 0

    parser.error(f"unknown command {command!r}")
    return 2


def _clean(graph_supplier, root: Path, metadata_dir: str | Path) -> int:
    """Delete declared outputs (concrete and currently realized), every
    output recorded in metadata, and the metadata store itself."""
    graph = graph_supplier() if callable(graph_supplier) else graph_supplier
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
