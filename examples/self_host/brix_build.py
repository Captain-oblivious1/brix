#!/usr/bin/env python3
"""brix builds brix: compile brix's own Rust engine with `brix.rust`.

    python3 examples/self_host/brix_build.py build     # -> rust/target/release/libbrix_py.so
    python3 examples/self_host/brix_build.py status

brix's engine (`rust/brix-py`, a PyO3 `cdylib`, on top of `rust/brix-core`) is
normally built by cargo directly. `brix.rust` wraps cargo, so brix can build its
*own* engine as a tracked build step — the self-hosting dogfood, and a live demo
that the Camp-2 "orchestrate a mature build tool" model works on a real cargo
workspace. `rust.cargo` reads `rust/brix-py/Cargo.toml` and infers the artifact
(`libbrix_py.so`) and a conservative source footprint (every `.rs` in the
workspace, so a `brix-core` change reruns cargo, plus the manifests and lock).

Plain incremental stack: cargo owns its `target/`, which atomic staging can't
redirect. Needs `cargo`. (This builds into the repo's real `rust/target/`; if
you want the built `.so` used by the Python package, copy it over
`src/brix/brix_py*.so`, as the release build does.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import Graph, cli, rust  # noqa: E402

# The repo root: paths are workspace-relative to it (rust/brix-py/Cargo.toml).
ROOT = Path(__file__).resolve().parents[2]


def create_graph() -> Graph:
    g = Graph()
    g.add(rust.cargo("rust/brix-py/Cargo.toml", root=ROOT))
    return g


if __name__ == "__main__":
    cli.main(create_graph, root=ROOT)
