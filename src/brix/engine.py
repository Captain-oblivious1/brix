"""The Rust engine boundary: run `build()` on `brix-core` via `brix_py`.

The Rust engine is the **default**: every builder's ``build()`` crosses
this boundary instead of running the Python executor — same graph, same
layer stack, same events, same ``BuildResult``. The user-facing API does
not change; this module adapts between it and the JSON wire pinned by
``docs/boundary-schema.md``. Set ``BRIX_ENGINE=py`` to force the Python
engine (kept working for one release after the flip; see
``docs/rust-port.md`` phase 4).

Graphs the Rust engine cannot take fall back silently to the Python
engine (see ``lowering.supports_rust_engine``): custom skin-registered
content-identity plugins, plus builders configured with a custom Python
cache store.

The extension is looked up as an installed ``brix_py`` module first, then
``$BRIX_RUST_ENGINE_LIB``, then the dev tree (``rust/target/{release,debug}``).
No extension anywhere: the *default* falls back to the Python engine (a
pure-Python checkout must keep building), but an explicit
``BRIX_ENGINE=rust`` raises — a silent fallback there would quietly
benchmark the wrong engine.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .plan import ExecutionNode, ExecutionPlan, NodeKind, NodeState, NodeStateTable
from .process import ProcessResult

if TYPE_CHECKING:
    from .builders import BuildResult
    from .report import Reporter

_UNSET = object()
_extension_cache = _UNSET


def _find_extension():
    try:
        import brix_py  # an installed wheel ships the extension as a module

        return brix_py
    except ImportError:
        pass
    candidates = []
    env = os.environ.get("BRIX_RUST_ENGINE_LIB")
    if env:
        candidates.append(Path(env))
    repo = Path(__file__).resolve().parents[2]
    for profile in ("release", "debug"):
        candidates.append(repo / "rust" / "target" / profile / "libbrix_py.so")
    for path in candidates:
        if path.is_file():
            loader = importlib.machinery.ExtensionFileLoader("brix_py", str(path))
            spec = importlib.util.spec_from_loader("brix_py", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    return None


def _extension():
    global _extension_cache
    if _extension_cache is _UNSET:
        _extension_cache = _find_extension()
    return _extension_cache


def rust_requested() -> bool:
    return os.environ.get("BRIX_ENGINE", "rust") == "rust"


class _EventAdapter:
    """Callable handed to the extension: parses each event JSON string and
    replays it onto a Python `Reporter`. Node refs become `ExecutionNode`
    shims — real ids/labels/kinds, no action (reporters render labels)."""

    def __init__(self, reporter: "Reporter", root: Path) -> None:
        self.reporter = reporter
        self.root = root
        self._nodes: dict[int, ExecutionNode] = {}

    def _node(self, ref: dict) -> ExecutionNode:
        node = self._nodes.get(ref["id"])
        if node is None:
            node = ExecutionNode(
                id=ref["id"], kind=NodeKind(ref["kind"]), label=ref["label"], action=None
            )
            self._nodes[ref["id"]] = node
        return node

    def __call__(self, event_json: str) -> None:
        event = json.loads(event_json)
        kind = event["event"]
        if kind == "build_begin":
            plan = ExecutionPlan(root=self.root)
            plan.nodes.extend(self._node(ref) for ref in event["plan"]["nodes"])
            self.reporter.build_begin(plan)
        elif kind == "node_started":
            self.reporter.node_started(self._node(event["node"]))
        elif kind == "process_started":
            self.reporter.process_started(self._node(event["node"]))
        elif kind == "node_completed":
            result = event.get("result")
            self.reporter.node_completed(
                self._node(event["node"]),
                NodeState(event["status"]),
                ProcessResult(**result) if result is not None else None,
                event.get("error"),
            )
        elif kind == "node_skipped":
            self.reporter.node_skipped(self._node(event["node"]), event["why"])
        elif kind == "collection_realized":
            self.reporter.collection_realized(
                self._node(event["node"]),
                event["members"],
                [self._node(ref) for ref in event["children"]],
            )
        elif kind == "node_inserted":
            self.reporter.node_inserted(self._node(event["node"]), event["reason"])
        # build_end is synthesized by maybe_build from the returned result,
        # which carries the final plan (the event stream's copy would not).


def _result_from_json(text: str, root: Path) -> "BuildResult":
    from .builders import BuildResult

    raw = json.loads(text)
    plan = ExecutionPlan(root=root)
    for ref in raw["plan"]["nodes"]:
        plan.nodes.append(
            ExecutionNode(
                id=ref["id"], kind=NodeKind(ref["kind"]), label=ref["label"], action=None
            )
        )
    states = NodeStateTable()
    for node_id, status in raw["states"].items():
        state = NodeState(status)
        if state is not NodeState.PENDING:
            states._status[int(node_id)] = state
    for node_id, result in raw["node_results"].items():
        states._result[int(node_id)] = ProcessResult(**result)
    return BuildResult(
        success=raw["success"],
        plan=plan,
        states=states,
        process_results=[ProcessResult(**r) for r in raw["process_results"]],
        errors=list(raw["errors"]),
    )


def _engine_for(graph) -> "object | None":
    """The extension module if the Rust engine should handle this graph,
    else None (Python engine). Shared dispatch: honors BRIX_ENGINE, the
    missing-extension rules, and the custom-identity fallback."""
    if not rust_requested():
        return None
    module = _extension()
    if module is None:
        if os.environ.get("BRIX_ENGINE") == "rust":
            raise RuntimeError(
                "BRIX_ENGINE=rust but the brix_py extension was not found; "
                "build it with `cargo build -p brix-py` (or set BRIX_RUST_ENGINE_LIB)"
            )
        return None  # defaulted, no extension: a pure-Python install still works
    from . import lowering

    if not lowering.supports_rust_engine(graph):
        return None  # custom skin-registered identity: Python engine owns it
    return module


def maybe_status(
    graph,
    root: Path,
    metadata_dir,
    staging_dir,
    targets: list[str] | None,
) -> dict | None:
    """`compute_status` through the core: the parsed StatusReport wire dict,
    or None to run the Python implementation. Raises what the Python
    implementation would raise (bad targets, unknown identity: ValueError)."""
    module = _engine_for(graph)
    if module is None:
        return None
    from . import lowering

    config = {
        "root": str(root),
        "metadata_dir": str(metadata_dir),
        "staging_dir": str(staging_dir),
        "targets": targets,
    }
    return json.loads(
        module.status(json.dumps(lowering.graph_to_spec(graph)), json.dumps(config))
    )


def maybe_clean(graph, root: Path, metadata_dir) -> int | None:
    """The CLI's `clean` through the core: files removed, or None to run the
    Python implementation."""
    module = _engine_for(graph)
    if module is None:
        return None
    from . import lowering

    config = {"root": str(root), "metadata_dir": str(metadata_dir)}
    raw = json.loads(
        module.clean(json.dumps(lowering.graph_to_spec(graph)), json.dumps(config))
    )
    return int(raw["removed"])


def maybe_build(
    builder,
    jobs: int | None = None,
    reporter: "Reporter | None" = None,
    targets: list[str] | None = None,
    hold: bool | None = None,
) -> "BuildResult | None":
    """The Rust-engine path for a builder's ``build()``, or None to run the
    Python engine (not requested, or the graph/builder can't cross yet)."""
    if not rust_requested():
        return None
    graph = builder.source_graph()
    module = _engine_for(graph)
    if module is None:
        return None
    specs = builder.layer_specs(hold=hold) if hold is not None else builder.layer_specs()
    if specs is None:
        return None  # a custom Python store can't cross; Python engine runs it
    from . import lowering

    spec_json = json.dumps(lowering.graph_to_spec(graph))
    config_json = json.dumps(
        {"root": str(builder.root), "jobs": jobs, "targets": targets, "layers": specs}
    )
    adapter = _EventAdapter(reporter, builder.root) if reporter is not None else None
    result = _result_from_json(module.build(spec_json, config_json, adapter), builder.root)
    if reporter is not None:
        reporter.build_end(result)
    return result
