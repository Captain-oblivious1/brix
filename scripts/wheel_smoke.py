#!/usr/bin/env python3
"""Build, install, and exercise a Brix wheel outside the source tree."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SMOKE = r"""
import sys
import tempfile
from pathlib import Path

import brix
from brix import CleanBuilder, ExternalProcessAction, Graph
from brix import engine

root = Path(tempfile.mkdtemp(prefix="brix-installed-build-"))
graph = Graph()
data = graph.data
graph.add(
    ExternalProcessAction(
        name="write",
        command_line="{python} -c {code} {out}",
        inputs={
            "python": data.tool(sys.executable),
            "code": data.value(
                "'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(\"wheel ok\")'"
            ),
        },
        outputs={"out": data.file("out.txt")},
    )
)
result = CleanBuilder(graph, root=root).build()
assert result.success, result.errors
assert (root / "out.txt").read_text() == "wheel ok"
assert engine._extension().__engine__ == "rust"
daemon = Path(engine._worker_daemon_config(None)["workerd"])
assert daemon.is_file(), daemon
# The shipped helpers ride embedded in the brix_py extension (no on-disk
# assets/ tree in a wheel); the asset store must resolve them from there.
from brix import assets

for _name in ("java/JavacWorker.java", "java/BootRepackager.java", "ts/tsc_listfiles.py"):
    assert len(assets.digest(_name)) == 64, _name
    assert Path(assets.resolve_path(_name)).is_file(), _name
"""


def run(*args: str, cwd: Path | None = None, expected: int = 0) -> None:
    completed = subprocess.run(args, cwd=cwd, check=False)
    if completed.returncode != expected:
        raise SystemExit(
            f"{args!r} returned {completed.returncode}, expected {expected}"
        )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="brix-wheel-smoke-") as temporary:
        temp = Path(temporary)
        wheels = temp / "wheels"
        venv = temp / "venv"
        run("maturin", "build", "--release", "--out", str(wheels), cwd=ROOT)
        run(sys.executable, "-m", "venv", str(venv))
        bindir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        python = bindir / ("python.exe" if sys.platform == "win32" else "python")
        workerd = bindir / ("brix-workerd.exe" if sys.platform == "win32" else "brix-workerd")
        wheel = next(wheels.glob("*.whl"))
        run(str(python), "-m", "pip", "install", "--no-deps", str(wheel), cwd=temp)
        run(str(python), "-c", SMOKE, cwd=temp)
        # No daemon arguments is an intentional usage error. Reaching the Rust
        # diagnostic proves the installed console launcher and extension work.
        run(str(workerd), cwd=temp, expected=2)
        print(f"wheel smoke passed: {wheel.name}")


if __name__ == "__main__":
    main()
