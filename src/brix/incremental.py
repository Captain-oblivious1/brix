"""The incremental layer: complete fresh nodes with their previous results.

A node is fresh when its fingerprint — command template, tool identities,
and the content hashes of every input — matches the record from the last
successful run, and its recorded outputs are still intact on disk. Fresh
nodes are completed with their previous ProcessResult (stdout/stderr replay
included) without running a process.

The fingerprint is deliberately a standalone function: the cache layer will
use the same computation as its cache key.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .actions import DeleteFilesAction, ExpandAll, ExternalProcessAction
from .data import DataAtom, DataExpr, FileData, GlobData, RegexData, ToolData, ValueData
from .layers import BuildLayer, NodeContext, NodeProposal
from .metadata import BuildMetadata, TimeAndHashChangeDetector
from .process import ProcessResult


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _atom_identity(atom: DataAtom) -> list:
    """Structural identity: what the atom refers to, not its content."""
    if isinstance(atom, FileData):
        return ["file", atom.path]
    if isinstance(atom, GlobData):
        if atom.exclude:  # only when present: pre-exclude identities unchanged
            return ["glob", atom.pattern.template, ["exclude", *atom.exclude]]
        return ["glob", atom.pattern.template]
    if isinstance(atom, RegexData):
        return ["regex", atom.regex.pattern]
    if isinstance(atom, ToolData):
        return ["tool", atom.name]
    if isinstance(atom, ValueData):
        return ["value"]  # the text is fingerprint material, not identity
    return ["other", repr(atom)]


def _expr_identity(expr: DataExpr) -> list:
    return [_atom_identity(a) for a in expr.atoms()]


def action_key(action: ExternalProcessAction) -> str:
    """Stable identity of an action across builds.

    Content-independent: input/output *shapes* and the command template.
    Changing a source's bytes or an option's text changes the fingerprint,
    not the key.
    """
    payload = {
        "command": action.command_line,
        "inputs": {k: _expr_identity(v) for k, v in action.inputs.items()},
        "outputs": {k: _expr_identity(v) for k, v in action.outputs.items()},
    }
    if action.env:
        payload["env"] = {k: _expr_identity(v) for k, v in action.env.items()}
    return _digest(payload)


def membership_key(action: ExpandAll) -> str:
    """Stable identity of an expansion point's collection across builds."""
    payload = {
        "element": action.element_name,
        "collection": _expr_identity(action.collection),
    }
    return _digest(payload)


def _content_key_material(content_key_name: str, physical, detector) -> list:
    """The fingerprint tail for a file carrying a content key:
    ``[tag, digest]`` (e.g. ``["jvm-abi@1", "<hex>"]``), or ``[tag:raw,
    byte_hash]`` when the lens cannot interpret the file. Deterministic for
    the same bytes, so a cross-machine disagreement can only miss, never make a
    stale hit. An unknown content-key name raises loudly (before the
    fallback)."""
    from .identity import get
    from .identity import tag as identity_tag

    lens = get(content_key_name)
    tag = identity_tag(lens)
    try:
        digest = detector.identity_digest(physical, lens)
    except Exception:
        return [f"{tag}:raw", detector.hash_file(physical)]
    return [tag, digest]


def action_fingerprint(
    action: ExternalProcessAction, ctx: NodeContext, detector: TimeAndHashChangeDetector
) -> str:
    """Everything that determines the action's outputs, hashed.

    Computed at node-ready time, when all inputs exist. This same value is
    the cache layer's key.
    """

    def atom_fp(atom: DataAtom) -> list:
        if isinstance(atom, FileData):
            physical = ctx.read_path(atom.path)
            if atom.content_key is not None:
                return [
                    "file",
                    atom.path,
                    *_content_key_material(atom.content_key, physical, detector),
                ]
            return ["file", atom.path, detector.hash_file(physical)]
        if isinstance(atom, GlobData):
            if atom.content_key is not None:
                material = [
                    "glob",
                    atom.pattern.template,
                    [
                        [logical, *_content_key_material(atom.content_key, physical, detector)]
                        for logical, physical, _ in ctx.find(atom.pattern)
                    ],
                ]
            else:
                material = [
                    "glob",
                    atom.pattern.template,
                    [
                        [logical, detector.hash_file(physical)]
                        for logical, physical, _ in ctx.find(atom.pattern)
                    ],
                ]
            if atom.exclude:  # only when present: pre-exclude fingerprints unchanged
                material.append(["exclude", *atom.exclude])
            return material
        if isinstance(atom, RegexData):
            return [
                "regex",
                atom.regex.pattern,
                [
                    [logical, detector.hash_file(physical)]
                    for logical, physical in ctx.iter_files()
                    if atom.match(logical) is not None
                ],
            ]
        if isinstance(atom, ToolData):
            resolved = atom.name if "/" in atom.name else shutil.which(atom.name)
            if resolved is None:
                return ["tool", atom.name, None, None]
            # workspace-relative tool paths resolve against the workspace;
            # absolute paths pass through the join unchanged
            return ["tool", atom.name, resolved, detector.stat_signature(ctx.workspace_root / resolved)]
        if isinstance(atom, ValueData):
            return ["value", atom.text]
        return ["other", repr(atom)]

    payload = {
        "command": action.command_line,
        "inputs": {k: [atom_fp(a) for a in v.atoms()] for k, v in action.inputs.items()},
    }
    if action.env:
        payload["env"] = {k: [atom_fp(a) for a in v.atoms()] for k, v in action.env.items()}
    if action.worker is not None:
        # The worker is part of the toolchain (its jar/interpreter determine the
        # output), so its identity joins the fingerprint with the same atom_fp
        # rules: a JDK upgrade or a rebuilt worker jar invalidates everything
        # this worker built, exactly like a compiler change.
        spec = action.worker
        payload["worker"] = {
            "command": spec.command_line,
            "inputs": {k: [atom_fp(a) for a in v.atoms()] for k, v in spec.inputs.items()},
        }
    # Discovered (scanned) dependencies — inputs the tool reported reading last
    # build that the graph didn't declare, e.g. C/C++ headers from a compiler
    # depfile. Content-hashed like any input, so editing one re-stales the
    # action. Absent on the first build (nothing scanned yet) and for actions
    # that emit no depfile, so it never perturbs an existing fingerprint.
    metadata = getattr(detector, "metadata", None)
    deps = metadata.discovered_deps(action_key(action)) if metadata is not None else None
    if deps:
        payload["deps"] = {
            path: detector.hash_file(ctx.read_path(path)) for path in sorted(deps)
        }
    return _digest(payload)


def scan_depfile(path: Path) -> list[str]:
    """Parse a Makefile-format dependency file (as emitted by ``cc -MMD -MF``)
    into the list of prerequisite paths. ``target: prereq1 prereq2 \\`` with
    line continuations and backslash-escaped spaces. Missing/unreadable → ``[]``.

    Assumes a single rule with no phony ``-MP`` targets (brix uses ``-MMD``, not
    ``-MP``). Only relative paths are kept: absolute prerequisites are system /
    toolchain files, already covered by the tool's own fingerprint, and would
    not resolve through the workspace overlay."""
    try:
        text = path.read_text()
    except OSError:
        return []
    text = text.replace("\\\n", " ")  # join line continuations
    _, sep, rhs = text.partition(":")  # drop the "target:" head
    if not sep:
        return []
    deps: list[str] = []
    for token in rhs.replace("\\ ", "\0").split():
        dep = token.replace("\0", " ")
        if dep and not dep.startswith("/"):
            deps.append(dep)
    return deps


def outputs_intact(
    record: dict, ctx: NodeContext, detector: TimeAndHashChangeDetector
) -> bool:
    """Do the recorded outputs still exist with their recorded content?

    Reads through the overlay: an output produced earlier in this build
    lives in staging; one committed by a previous build lives in the
    workspace. Either satisfies freshness.
    """
    for path, stored in record.get("outputs", {}).items():
        if detector.hash_file(ctx.read_path(path)) != stored:
            return False
    return True


def output_hashes(
    action: ExternalProcessAction, ctx: NodeContext, detector: TimeAndHashChangeDetector
) -> dict:
    hashes: dict[str, str | None] = {}
    for expr in action.outputs.values():
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.pattern.is_concrete:
                hashes[atom.path] = detector.hash_file(ctx.read_path(atom.path))
            elif isinstance(atom, GlobData):
                for logical, physical, _ in ctx.find(atom.pattern):
                    hashes[logical] = detector.hash_file(physical)
    return hashes


def remove_outputs(action: ExternalProcessAction, ctx: NodeContext) -> None:
    for expr in action.outputs.values():
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.pattern.is_concrete:
                target = ctx.output_root / atom.path
                if target.is_file():  # directory outputs are left in place
                    target.unlink()
            elif isinstance(atom, GlobData):
                for path, _ in atom.pattern.find(ctx.output_root):
                    (ctx.output_root / path).unlink(missing_ok=True)


class IncrementalLayer(BuildLayer):
    """Skip fresh nodes; record fingerprints and results after real runs."""

    def __init__(self, metadata: BuildMetadata, detector: TimeAndHashChangeDetector) -> None:
        self.metadata = metadata
        self.detector = detector
        # Current-build membership, applied to metadata only if the build
        # succeeds: a failed build must re-detect deletions next time.
        self._pending_memberships: dict[str, list[str]] = {}

    # -- per-node ------------------------------------------------------------

    def around_node(self, node, ctx, inner):
        action = node.action
        if not isinstance(action, ExternalProcessAction):
            return inner(ctx)
        key = action_key(action)
        fingerprint = action_fingerprint(action, ctx, self.detector)
        record = self.metadata.action_record(key)
        if (
            record
            and record.get("ok", True)
            and record.get("fingerprint") == fingerprint
            and outputs_intact(record, ctx, self.detector)
        ):
            replayed = dict(record["result"])
            replayed["source"] = "replayed"
            return ProcessResult(**replayed)
        result = inner(ctx)
        if result.ok:
            if action.depfile is not None:
                # Fold the tool's reported inputs (e.g. C/C++ headers) into the
                # fingerprint, then recompute it so this same build's record is
                # already dep-aware — the next unchanged build is fresh, with no
                # spurious extra rebuild. Runs after a cache hit too (the depfile
                # is a restored output), so a cache-fed machine also learns them.
                self.metadata.set_discovered_deps(
                    key, scan_depfile(ctx.read_path(action.depfile))
                )
                fingerprint = action_fingerprint(action, ctx, self.detector)
            self.metadata.record_action(
                key,
                {
                    "ok": True,
                    "fingerprint": fingerprint,
                    "result": asdict(result),
                    "outputs": output_hashes(action, ctx, self.detector),
                },
            )
        else:
            # A clean build would not have produced these outputs; neither
            # may we. The failure itself is recorded — never as fresh (a
            # failed action always reruns), but status/errors can report it.
            remove_outputs(action, ctx)
            self.metadata.record_action(
                key,
                {"ok": False, "fingerprint": fingerprint, "result": asdict(result), "outputs": {}},
            )
        return result

    # -- membership ------------------------------------------------------------

    def on_collection_realized(self, node, members, ctx):
        action = node.action
        if not isinstance(action, ExpandAll):
            return None
        key = membership_key(action)
        previous = self.metadata.membership(key)
        self._pending_memberships[key] = list(members)
        if not previous:
            return None
        removed = sorted(set(previous) - set(members))
        proposals: list[NodeProposal] = []
        for member in removed:
            vanished = action.child.substitute({action.element_name: member})
            stale_outputs = self._reap_vanished(vanished)
            if stale_outputs:
                proposals.append(NodeProposal(DeleteFilesAction(stale_outputs)))
        return proposals or None

    def _reap_vanished(self, action) -> list[str]:
        """Concrete stale outputs under a vanished expansion member, forgetting
        its fingerprint records along the way.

        Recurses through *nested* expansions. Their inner collections never
        realize this build — the whole subtree is pruned when the outer member
        disappears — so their membership is read back from metadata instead: the
        inner ``membership_key`` is deterministic from the substituted
        collection, so it matches exactly what the previous build recorded for
        this member (e.g. a deleted video's scene list).

        Reads, but never *mutates*, membership records. A failed build must
        re-detect this deletion next time via the outer membership (which is
        only committed on success), and that re-detection walks back in here and
        needs the inner lists intact. ``forget_action`` is idempotent, so
        re-forgetting on a later retry is harmless. If an inner membership was
        never recorded (a prior failure, or nesting added since), that subtree
        can't be enumerated and is left for a future build — the one
        unrecoverable case, degrading to the old skip rather than guessing."""
        if isinstance(action, ExternalProcessAction):
            self.metadata.forget_action(action_key(action))
            return [
                atom.path
                for expr in action.outputs.values()
                for atom in expr.atoms()
                if isinstance(atom, FileData) and atom.pattern.is_concrete
            ]
        if isinstance(action, ExpandAll):
            stale: list[str] = []
            for member in self.metadata.membership(membership_key(action)) or []:
                child = action.child.substitute({action.element_name: member})
                stale += self._reap_vanished(child)
            return stale
        return []  # DeleteFilesAction or other: nothing to reap

    def on_build_end(self, result, ctx):
        if result.success:
            for key, members in self._pending_memberships.items():
                self.metadata.set_membership(key, members)
        self._pending_memberships.clear()
        self.metadata.save()

