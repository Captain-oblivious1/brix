"""Emit the Python lowering of a `rust` skin graph as JSON."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, rust  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        rust.CargoBuildAction(
            manifest="crates/widget/Cargo.toml",
            artifact=d.file("target/debug/libwidget.a"),
            sources=d.glob("crates/{name}.rs"),
            cargo=d.tool("cargo-nightly"),
            release=False,
            offline=True,
            options=["--locked", "--features", "ffi"],
            env={"RUSTFLAGS": "-C debuginfo=1", "WIDGET_MODE": "oracle"},
            env_used=["CI"],
        ),
        # Bare defaults: release=True (the --release branch), no offline, no
        # options/env/env_used, default cargo tool — the otherwise-uncovered path.
        rust.CargoBuildAction(
            manifest="crates/bare/Cargo.toml",
            artifact=d.file("target/release/libbare.a"),
            sources=d.glob("crates/bare/{name}.rs"),
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
