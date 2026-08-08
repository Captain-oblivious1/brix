"""One-shot onboarding CLI: dump an existing build and emit ``brix_build.py``.

    python -m brix.onboard <project-dir> [-o brix_build.py] [--force]
                           [--ir brix-ir.json] [--offline]
                           [--java-installations PATH[,PATH...]]

The front door for bringing a project under brix: it runs the Gradle *dumper*
(introspect the resolved build model → tool-neutral IR) and pipes the IR straight
into the *emitter* (→ ``brix_build.py``), then prints the semantic gaps the IR
cannot carry (main classes, compiler plugins, toolchain JDKs) so a human knows
exactly what to fill in. The intermediate IR is kept only if ``--ir`` is given.

Two-step access remains for debugging: ``python -m brix.onboard.gradle`` (dump
only) and ``python -m brix.onboard.emit`` (emit from a saved IR).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .emit import EmitResult, emit_build_file
from .gradle import run_dumper


def run_onboard(
    project_dir,
    *,
    out=None,
    ir_out=None,
    force: bool = False,
    offline: bool = False,
    java_installations=(),
    ir: dict | None = None,
) -> tuple[Path, EmitResult]:
    """Dump ``project_dir`` and emit a ``brix_build.py``, returning
    ``(out_path, EmitResult)``.

    ``out``                where to write the build file (default:
                           ``<project>/brix_build.py``).
    ``force``              overwrite an existing ``out`` (default: refuse).
    ``ir_out``             keep the intermediate IR JSON here (default: discard).
    ``offline`` / ``java_installations``  forwarded to the dumper.
    ``ir``                 use this IR instead of running the dumper (for tests;
                           skips Gradle entirely).
    """
    project_dir = Path(project_dir).resolve()
    out = Path(out).resolve() if out else project_dir / "brix_build.py"

    # Fail fast, before the (expensive) dump, rather than after.
    if out.exists() and not force:
        raise FileExistsError(
            f"{out} already exists; pass force=True (--force) or a different -o/out"
        )

    if ir is None:
        if ir_out is not None:
            ir = run_dumper(
                project_dir, out_path=ir_out,
                offline=offline, java_installations=java_installations,
            )
        else:
            # No IR requested to be kept: dump to a temp file and discard it.
            with tempfile.TemporaryDirectory() as tmp:
                ir = run_dumper(
                    project_dir, out_path=Path(tmp) / "brix-ir.json",
                    offline=offline, java_installations=java_installations,
                )

    result = emit_build_file(ir)
    out.write_text(result.source)
    return out, result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="brix.onboard", description=__doc__)
    parser.add_argument("project_dir", help="root of the build to onboard")
    parser.add_argument(
        "-o", "--out", default=None,
        help="brix_build.py output path (default: <project>/brix_build.py)",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file",
    )
    parser.add_argument(
        "--ir", default=None, help="also keep the intermediate IR JSON here",
    )
    parser.add_argument("--offline", action="store_true", help="pass --offline to Gradle")
    parser.add_argument(
        "--java-installations", default="",
        help="comma-separated JDK paths for toolchain resolution",
    )
    args = parser.parse_args(argv)

    installs = [p for p in args.java_installations.split(",") if p]
    try:
        out_path, result = run_onboard(
            args.project_dir,
            out=args.out,
            ir_out=args.ir,
            force=args.force,
            offline=args.offline,
            java_installations=installs,
        )
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"wrote {out_path}", file=sys.stderr)
    print(
        f"emitted {len(result.emitted_modules)} module(s), "
        f"skipped {len(result.skipped_modules)}, "
        f"bridged {len(result.bridged_nodes)}",
        file=sys.stderr,
    )
    # The gaps a human must close (TODOs) and the wiring decisions taken.
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
