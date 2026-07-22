"""The starter templates under examples/templates/ must actually work.

Each template is a self-contained project a user copies out and builds with an
installed brix. These tests copy a template to a temp dir (so nothing pollutes
the repo, and the copied-out path is what runs), point it at this repo's ``src``
via PYTHONPATH, and exercise it as a user would — a tool-free structural check
that the ``brix_build.py`` parses and plans, plus a gated end-to-end build when the
language's toolchain is present.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "examples" / "templates"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # The copied-out template imports brix as if installed; point it here.
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "brix_build.py", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _copy(template: str, dest: Path) -> Path:
    out = dest / template
    shutil.copytree(TEMPLATES / template, out)
    return out


@pytest.mark.parametrize("template", ["java-service", "typescript-service"])
def test_template_parses_and_plans(tmp_path, template):
    # Tool-free: `graph` imports the build file and builds/plans the graph
    # without invoking any compiler, so it runs everywhere.
    proj = _copy(template, tmp_path)
    result = _run(proj, "graph")
    assert result.returncode == 0, result.stderr
    assert "->" in result.stdout  # the plan printed at least one action


_have_jdk = all(shutil.which(t) for t in ("javac", "jar", "java"))


@pytest.mark.skipif(not _have_jdk, reason="no JDK (javac/jar/java)")
def test_java_template_builds_and_runs(tmp_path):
    proj = _copy("java-service", tmp_path)

    build = _run(proj, "build")
    assert build.returncode == 0, build.stderr
    assert (proj / "build/libs/app.jar").is_file()

    # A second build replays (nothing changed).
    rebuild = _run(proj, "build")
    assert rebuild.returncode == 0, rebuild.stderr
    assert "replayed" in rebuild.stdout

    run = _run(proj, "run", "app")
    assert run.returncode == 0, run.stderr
    assert "brix-built Java service" in run.stdout


_have_ts = shutil.which("tsc") is not None and shutil.which("node") is not None


@pytest.mark.skipif(not _have_ts, reason="no tsc/node")
def test_typescript_template_builds_and_runs(tmp_path):
    proj = _copy("typescript-service", tmp_path)

    build = _run(proj, "build")
    assert build.returncode == 0, build.stderr
    assert (proj / "dist/main.js").is_file()

    run = _run(proj, "run", "app")
    assert run.returncode == 0, run.stderr
    assert "brix-built TypeScript service" in run.stdout
