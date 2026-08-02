"""CLI: dump a Gradle build's onboarding IR.

    python -m brix.onboard.gradle <project-dir> [-o out.json] [--offline]
                                  [--java-installations PATH[,PATH...]]
"""

from __future__ import annotations

import argparse
import sys

from .runner import run_dumper


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="brix.onboard.gradle", description=__doc__)
    parser.add_argument("project_dir", help="root of the Gradle build to dump")
    parser.add_argument("-o", "--out", default=None, help="IR JSON output path")
    parser.add_argument("--offline", action="store_true", help="pass --offline to Gradle")
    parser.add_argument(
        "--java-installations",
        default="",
        help="comma-separated JDK paths for toolchain resolution",
    )
    args = parser.parse_args(argv)

    installs = [p for p in args.java_installations.split(",") if p]
    ir = run_dumper(
        args.project_dir,
        out_path=args.out,
        offline=args.offline,
        java_installations=installs,
    )
    print(
        f"modules={len(ir['modules'])} nodes={len(ir['nodes'])} "
        f"(gradle {ir['gradle_version']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
