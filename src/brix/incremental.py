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
    return _digest(payload)


def membership_key(action: ExpandAll) -> str:
    """Stable identity of an expansion point's collection across builds."""
    payload = {
        "element": action.element_name,
        "collection": _expr_identity(action.collection),
    }
    return _digest(payload)


def action_fingerprint(
    action: ExternalProcessAction, ctx: NodeContext, detector: TimeAndHashChangeDetector
) -> str:
    """Everything that determines the action's outputs, hashed.

    Computed at node-ready time, when all inputs exist. This same value is
    the cache layer's key.
    """

    def atom_fp(atom: DataAtom) -> list:
        if isinstance(atom, FileData):
            return ["file", atom.path, detector.hash_file(ctx.read_path(atom.path))]
        if isinstance(atom, GlobData):
            return [
                "glob",
                atom.pattern.template,
                [
                    [logical, detector.hash_file(physical)]
                    for logical, physical, _ in ctx.find(atom.pattern)
                ],
            ]
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
    return _digest(payload)


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
            self.metadata.forget_action(action_key(vanished))
            stale_outputs = [
                atom.path
                for expr in vanished.outputs.values()
                for atom in expr.atoms()
                if isinstance(atom, FileData) and atom.pattern.is_concrete
            ]
            if stale_outputs:
                proposals.append(NodeProposal(DeleteFilesAction(stale_outputs)))
        return proposals or None

    def on_build_end(self, result, ctx):
        if result.success:
            for key, members in self._pending_memberships.items():
                self.metadata.set_membership(key, members)
        self._pending_memberships.clear()
        self.metadata.save()

