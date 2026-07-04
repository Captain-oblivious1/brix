"""Reporting: the engine never prints — reporters render events.

The scheduler emits events to a `Reporter`, the observational twin of
`BuildLayer`: it sees everything and changes nothing. Output styles are
Reporter implementations chosen by the client. The default is silence —
``build()`` returns a `BuildResult`, so an exit-code-only client needs no
reporter at all.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from .builders import BuildResult
    from .plan import ExecutionNode, ExecutionPlan, NodeState
    from .process import ProcessResult


class Reporter:
    """Base reporter: silent. Subclass and override what you care about."""

    def build_begin(self, plan: "ExecutionPlan") -> None:
        """The build is starting."""

    def node_started(self, node: "ExecutionNode") -> None:
        """A process node was handed to the layer stack."""

    def node_completed(
        self,
        node: "ExecutionNode",
        status: "NodeState",
        result: "ProcessResult | None",
        error: str | None = None,
    ) -> None:
        """A node finished (DONE or FAILED). ``result`` is None when the
        failure happened outside a process (e.g. an expansion error)."""

    def node_skipped(self, node: "ExecutionNode", why: str) -> None:
        """A node was skipped (failure cascade or unreachable)."""

    def collection_realized(
        self, node: "ExecutionNode", members: list[str], children: "list[ExecutionNode]"
    ) -> None:
        """An expansion point realized and expanded into concrete nodes."""

    def node_inserted(self, node: "ExecutionNode", reason: str) -> None:
        """The scheduler inserted a layer-proposed node."""

    def build_end(self, result: "BuildResult") -> None:
        """The build finished; ``result`` is the full BuildResult."""


class ConsoleReporter(Reporter):
    """Live output as the build runs — the classic make-style stream.

    Results that were not produced by a fresh process are marked with
    their provenance, e.g. ``ok: compile foo (replayed)``. Pass
    ``show_replays=False`` to print only real work: successful replayed
    results are suppressed entirely (replayed *failures* still print).
    """

    def __init__(self, stream: TextIO | None = None, show_replays: bool = True) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.show_replays = show_replays

    def _say(self, message: str) -> None:
        print(message, file=self.stream)

    def collection_realized(self, node, members, children) -> None:
        self._say(f"{node.label} -> {len(children)} action(s)")

    def node_inserted(self, node, reason) -> None:
        self._say(f"insert: {node.label}")

    def node_completed(self, node, status, result, error=None) -> None:
        if result is None:
            suffix = f": {error}" if error else ""
            self._say(f"FAILED: {node.label}{suffix}")
            return
        if not self.show_replays and result.ok and result.source != "process":
            return
        line = result.summary()
        if result.source != "process":
            line += f" ({result.source})"
        self._say(line)
        if result.ok:
            if result.stdout:
                self._say(result.stdout.rstrip("\n"))
        else:
            for text in (result.stdout, result.stderr):
                if text:
                    self._say(text.rstrip("\n"))

    def node_skipped(self, node, why) -> None:
        self._say(f"skip: {node.label} ({why})")

    def build_end(self, result) -> None:
        self._say(result.summary())


class ErrorsAtEndReporter(Reporter):
    """Silent while the build runs; prints failures once it finishes."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    def _say(self, message: str) -> None:
        print(message, file=self.stream)

    def build_end(self, result) -> None:
        for process_result in result.process_results:
            if not process_result.ok:
                self._say(process_result.summary())
                for text in (process_result.stdout, process_result.stderr):
                    if text:
                        self._say(text.rstrip("\n"))
        for error in result.errors:
            if not any(error == r.summary() for r in result.process_results):
                self._say(error)
        self._say(result.summary())
