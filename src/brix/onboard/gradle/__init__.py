"""Gradle dumper — the Gradle frontend of the brix onboarder.

Introspects Gradle's already-resolved project/task model (via an injected init
script and the ``brixDumpModel`` task) and emits the tool-neutral onboarding IR.
See ``dump.gradle`` for the model walk and ``runner.py`` for the Python driver.

Usage:  python -m brix.onboard.gradle <project-dir> [-o out.json]
"""

from .runner import DUMP_TASK, INIT_SCRIPT, gradle_command, run_dumper

__all__ = ["run_dumper", "gradle_command", "INIT_SCRIPT", "DUMP_TASK"]
