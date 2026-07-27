"""Dump the lowering of an action with env_used covering a set var (with a
quote + non-ASCII), a set-empty var, and an unset var — so the TS oracle can
prove the _brix_env_used snapshot byte-matches Python's json.dumps for real
values, not just nulls."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import ExternalProcessAction, Graph  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    g.add(
        ExternalProcessAction(
            name="probe",
            command_line="{t}",
            inputs={"t": g.data.tool("true")},
            outputs={},
            env_used=["BRIX_TEST_SET", "BRIX_TEST_EMPTY", "BRIX_TEST_UNSET"],
        )
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
