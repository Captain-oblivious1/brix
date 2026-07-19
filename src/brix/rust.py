"""Typed Rust actions — wrap cargo.

Rust ships its own build tool (**cargo**), which owns crate and dependency
resolution and within-crate incremental compilation. brix does not replace that:
`cargo` (the adapter) and `CargoBuildAction` run ``cargo build`` as one tracked
action, so a Rust crate becomes a node in a polyglot brix graph — result-cached,
lazy-loaded in a monorepo, and wired to other languages by its built artifact
(a ``staticlib``/``cdylib`` a C/C++/JNI/WASM consumer links).

This is the *orchestration* model, distinct from `brix.cpp`/`brix.java` (which
own compilation). Two honest consequences:

* **Granularity is crate-level.** brix reruns cargo when any *declared* input
  changed; cargo does the fine-grained work within. Declare the footprint
  conservatively — for a cargo **workspace** that means every crate's sources
  (a change in a path-dependency must rerun cargo), which is why `cargo` globs
  the whole workspace's ``.rs`` by default, not just the built member's.
* **Not atomic-stage-able.** cargo writes to its own ``target/``, a resolver
  brix's atomic overlay can't redirect (like ``tsc``'s ``node_modules`` or a C
  ``#include`` of a generated header), so a cargo action wants the plain
  incremental stack, not atomic staging.

``env=`` sets (and fingerprints) variables — ``RUSTFLAGS``, ``CARGO_NET_OFFLINE``
— and ``env_used=`` promotes an *inherited* variable to fingerprint material
without setting it (e.g. an ambient ``RUSTFLAGS`` a build relies on), so changing
it re-stales the build. cargo is env-sensitive, so `brix.rust` is the first
wrapper to expose both.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .actions import Action, ExternalProcessAction
from .data import DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData

__all__ = ['CargoBuildAction', 'cargo']


class CargoBuildAction(Action):
    """Run ``cargo build`` for a crate, tracking its sources and built artifact.

    ``manifest`` is the ``Cargo.toml`` path; ``artifact`` the built file brix
    tracks as this action's output (e.g. ``target/release/libfoo.a``); ``sources``
    the tracked input footprint (cargo finds them itself — they are not command
    arguments). Prefer the `cargo` adapter, which infers ``artifact``/``sources``
    from the manifest.
    """

    def __init__(
        self,
        *,
        manifest: str,
        artifact: str | FileData,
        sources: DataExpr,
        cargo: ToolData | None = None,
        release: bool = True,
        offline: bool = False,
        options: list[str] | None = None,
        env: dict[str, DataExpr | str] | None = None,
        env_used: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.manifest = manifest
        self.artifact = artifact if isinstance(artifact, FileData) else FileData(artifact)
        self.sources = sources
        self.cargo = cargo if cargo is not None else ToolData("cargo")
        self.release = release
        self.offline = offline
        self.options = options
        self.env = env
        self.env_used = tuple(env_used or ())

    def lower(self) -> list[Action]:
        flags: list[str] = []
        if self.release:
            flags.append("--release")
        if self.offline:
            # Air-gapped/reproducible: use only the local registry cache, fail
            # loudly rather than reach the network.
            flags.append("--offline")
        flags += list(self.options or [])
        return [
            ExternalProcessAction(
                name=f"cargo build -> {self.artifact.path}",
                # cargo self-resolves crate sources and its target dir from the
                # manifest; the sources are tracked inputs, not command args.
                command_line="{cargo} build --manifest-path {manifest} {options}",
                inputs={
                    "cargo": self.cargo,
                    "manifest": FileData(self.manifest),
                    "sources": self.sources,
                    "options": ValueData(" ".join(flags)),
                },
                outputs={"artifact": self.artifact},
                env=self.env or {},
                env_used=self.env_used,
            )
        ]


def _workspace_root(manifest_dir: Path) -> Path:
    """The highest ancestor (inclusive) whose ``Cargo.toml`` declares a
    ``[workspace]`` — cargo writes a member's output into the *workspace's*
    ``target/``, not the member's. Falls back to the crate dir for a standalone
    crate."""
    root = manifest_dir
    probe = manifest_dir
    while True:
        cargo_toml = probe / "Cargo.toml"
        if cargo_toml.is_file():
            try:
                if "workspace" in tomllib.loads(cargo_toml.read_text()):
                    root = probe
            except (OSError, tomllib.TOMLDecodeError):
                pass
        if probe.parent == probe:
            break
        probe = probe.parent
    return root


def _infer_artifact(
    target_dir: Path, profile: str, lib_name: str, pkg_name: str,
    crate_types: list[str], manifest_dir: Path, base: Path,
) -> str:
    if "staticlib" in crate_types:
        filename = f"lib{lib_name}.a"
    elif "cdylib" in crate_types or "dylib" in crate_types:
        filename = f"lib{lib_name}.so"  # Linux; .dylib/.dll on mac/win — pass artifact= there
    elif (manifest_dir / "src" / "main.rs").is_file():
        filename = pkg_name  # a binary crate
    else:
        filename = f"lib{lib_name}.rlib"
    return (target_dir / profile / filename).relative_to(base).as_posix()


def _default_sources(base: Path, ws_root: Path, manifest: Path) -> DataExpr:
    ws_rel = ws_root.relative_to(base).as_posix()
    prefix = "" if ws_rel == "." else f"{ws_rel}/"
    atoms: list[DataExpr] = [GlobData(f"{prefix}{{name}}.rs")]
    # The manifests and lockfile matter too (a dep bump changes the build).
    for extra in {manifest, ws_root / "Cargo.toml"}:
        if extra.is_file():
            atoms.append(FileData(extra.relative_to(base).as_posix()))
    # Track Cargo.lock unconditionally (even before it exists): cargo creates it
    # on first build, and a dep-version change rewrites it. Declaring it always
    # keeps the input set stable across builds (a missing file hashes as absent).
    atoms.append(FileData((ws_root / "Cargo.lock").relative_to(base).as_posix()))
    return DataUnion(*atoms)


def cargo(
    manifest: str = "Cargo.toml",
    *,
    root: str | Path = ".",
    artifact: str | None = None,
    sources: DataExpr | None = None,
    release: bool = True,
    offline: bool = False,
    options: list[str] | None = None,
    env: dict[str, DataExpr | str] | None = None,
    env_used: list[str] | tuple[str, ...] | None = None,
    cargo_tool: ToolData | None = None,
) -> CargoBuildAction:
    """Build a Rust crate with cargo, inferring the artifact and source footprint
    from its ``Cargo.toml`` (the `java.pom`/`cpp.pkg_config` *adopt-the-manifest*
    pattern — Cargo.toml stays the single source of truth for the crate; brix.py
    only says how the crate plugs into the graph).

    Reads the manifest now (per build, like the Maven adapters), relative to
    ``root`` (the workspace root the build runs from). Infers the output artifact
    from ``[lib].crate-type`` (``staticlib`` -> ``lib<name>.a``, ``cdylib`` ->
    ``lib<name>.so``, a ``src/main.rs`` -> the binary, else ``lib<name>.rlib``)
    under the *workspace's* ``target/<profile>/``; pass ``artifact=`` to override.
    ``sources`` defaults to every ``.rs`` under the workspace plus the manifests
    and ``Cargo.lock`` — conservative on purpose (crate-level granularity).
    """
    base = Path(root)
    manifest_path = base / manifest
    parsed = tomllib.loads(manifest_path.read_text())
    pkg = parsed.get("package", {})
    pkg_name = pkg.get("name", manifest_path.parent.name)
    lib = parsed.get("lib", {})
    lib_name = lib.get("name") or pkg_name.replace("-", "_")
    crate_types = lib.get("crate-type", [])
    profile = "release" if release else "debug"

    manifest_dir = manifest_path.parent
    ws_root = _workspace_root(manifest_dir)
    target_dir = ws_root / "target"

    if artifact is None:
        artifact = _infer_artifact(
            target_dir, profile, lib_name, pkg_name, crate_types, manifest_dir, base
        )
    if sources is None:
        sources = _default_sources(base, ws_root, manifest_path)

    return CargoBuildAction(
        manifest=manifest,
        artifact=artifact,
        sources=sources,
        release=release,
        offline=offline,
        options=options,
        env=env,
        env_used=env_used,
        cargo=cargo_tool,
    )
