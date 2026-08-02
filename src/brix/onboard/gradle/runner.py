"""Drive the Gradle dumper and return the onboarding IR.

This is the thin Python side of the Gradle *frontend*: it shells out to the
target's own Gradle (the wrapper if present), injecting ``dump.gradle`` as an
init script and running the ``brixDumpModel`` task, then reads back the IR JSON.
No JVM is embedded in Python — we let Gradle do the extraction it alone can do,
and cross the boundary with a file.

Why shell out rather than the Tooling API: it keeps the tool dependency-free on
the Python side and runs deterministically on an air-gapped machine where Gradle
already works. The IR JSON is the seam — the emitter is tested against captured
IR fixtures without needing Gradle at all.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

#: The init script that does the actual model walk (see dump.gradle).
INIT_SCRIPT = Path(__file__).with_name("dump.gradle")

#: The task the init script registers on the root project.
DUMP_TASK = "brixDumpModel"


def gradle_command(project_dir: Path) -> list[str]:
    """Prefer the project's wrapper (pins the Gradle version the build expects);
    fall back to a ``gradle`` on PATH."""
    wrapper = project_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
    return [str(wrapper)] if wrapper.exists() else ["gradle"]


def run_dumper(
    project_dir,
    out_path=None,
    *,
    offline: bool = False,
    java_installations=(),
    extra_args=(),
    check: bool = True,
) -> dict:
    """Run the dumper against ``project_dir`` and return the parsed IR.

    ``out_path``          where the IR JSON is written (default: a sibling
                          ``brix-ir.json`` in the project root).
    ``offline``           pass ``--offline`` (only works if all dependency
                          metadata is already cached; otherwise resolution
                          fails and classpaths come back empty).
    ``java_installations`` paths to JDKs, forwarded so Gradle's toolchain
                          resolution can satisfy a build that pins a specific
                          Java version without downloading one.
    ``extra_args``        extra Gradle CLI args appended verbatim.
    """
    project_dir = Path(project_dir).resolve()
    out_path = Path(out_path).resolve() if out_path else (project_dir / "brix-ir.json")

    cmd = gradle_command(project_dir) + [
        "--init-script", str(INIT_SCRIPT),
        # The dump task captures the live Project/Task model, which the
        # configuration cache cannot serialize — so it is disabled here.
        "--no-configuration-cache",
        "--console=plain",
        "-q",
        f"-Dbrix.dump.out={out_path}",
    ]
    if offline:
        cmd.append("--offline")
    if java_installations:
        paths = ",".join(str(Path(p)) for p in java_installations)
        cmd.append(f"-Porg.gradle.java.installations.paths={paths}")
    cmd += list(extra_args)
    cmd.append(DUMP_TASK)

    subprocess.run(cmd, cwd=project_dir, check=check)
    return json.loads(out_path.read_text())
