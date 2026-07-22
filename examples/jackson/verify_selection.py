#!/usr/bin/env python3
"""Prove cross-module suite selection with a reversible core implementation edit."""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("brix_jackson_example", HERE / "brix_build.py")
assert spec and spec.loader
example = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = example
spec.loader.exec_module(example)

from brix import Reporter  # noqa: E402


class Started(Reporter):
    def __init__(self) -> None:
        self.labels: list[str] = []

    def process_started(self, node) -> None:
        self.labels.append(node.label)


def contains(labels: list[str], fragment: str) -> bool:
    return any(fragment in label for label in labels)


def main() -> None:
    source = example.ROOT / "jackson-core/src/main/java/com/fasterxml/jackson/core/JsonFactory.java"
    original = source.read_text()
    probe = "\n    private static int brixSelectionProbe() { return 41; }\n"
    if probe in original:
        raise RuntimeError(f"selection probe already exists in {source}")

    baseline = example.builder().build()
    if not baseline.success:
        raise RuntimeError(f"baseline build failed: {baseline.errors}")

    try:
        closing_brace = original.rfind("}")
        source.write_text(original[:closing_brace] + probe + original[closing_brace:])
        started = Started()
        changed = example.builder().build(reporter=started)
        if not changed.success:
            raise RuntimeError(f"changed build failed: {changed.errors}")

        expected_to_run = (
            "build/classes/core",
            "jar build/libs/jackson-core.jar",
            "run core smoke tests",
            "run databind smoke tests",
        )
        expected_to_replay = (
            "build/classes/annotations",
            "build/classes/databind",
            "compile annotations smoke tests",
            "run annotations smoke tests",
            "compile core smoke tests",
            "compile databind smoke tests",
        )
        missing = [item for item in expected_to_run if not contains(started.labels, item)]
        unexpected = [item for item in expected_to_replay if contains(started.labels, item)]
        if missing or unexpected:
            raise AssertionError(
                f"wrong selection; missing={missing}, unexpected={unexpected}, "
                f"started={started.labels}"
            )
        print("selection verified:")
        for label in started.labels:
            print(f"  ran: {label}")
    finally:
        source.write_text(original)
        restored = example.builder().build()
        if not restored.success:
            raise RuntimeError(f"failed to restore fresh build state: {restored.errors}")


if __name__ == "__main__":
    main()
