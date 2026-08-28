"""Target selection: build only what produces the requested outputs.

Demand-driven, on the logical graph, before planning. Each target path is
matched against action output patterns; a match extracts *bindings*
(``output/videos/wealth.mpg`` vs ``output/videos/{video}.mpg`` gives
``video=wealth``), which substitute into the action — constraining its
collections, so an expansion over all videos narrows to one. The
constrained action's inputs become new needs, and the pass repeats until
nothing new is selected.

Selection is conservative: where bindings can't be proven (regex atoms,
uncertain overlaps), the producer is kept unconstrained. One action can be
selected several times under different bindings (two targeted videos →
two constrained expansions); duplicates share incremental records, since
expanded concrete children are identical either way.

When several producers match one need, the most specific match wins —
make's shortest-stem rule. Without it, suffix pipelines diverge:
``{video}.mpg`` also matches ``wealth.raw.mpg`` as ``video=wealth.raw``,
manufacturing an endless ``wealth.raw.raw...`` chain; the ``{video}.raw.mpg``
producer (stem ``wealth``) is the specific one.
"""

from __future__ import annotations

import re

from .actions import Action, ExpandAll, Expandable
from .data import DataExpr, FileData, GlobData, RegexData, _fill_named_groups
from .patterns import overlaps

_PROBE = "\x00target\x00"


def _extract_bindings(out_atom, need_atom) -> dict[str, str] | None:
    """How does producing ``out_atom`` help ``need_atom``?

    None: unrelated. {}: related, no constraint provable. Otherwise the
    bindings that specialize the producer to this need.
    """
    if isinstance(out_atom, RegexData) or isinstance(need_atom, RegexData):
        from .planner import _atoms_overlap

        return {} if _atoms_overlap(out_atom, need_atom) else None
    out_pattern, need_pattern = out_atom.pattern, need_atom.pattern
    probed = need_pattern.substitute({name: _PROBE for name in need_pattern.binding_names})
    matched = out_pattern.match(probed)
    if matched is not None:
        return {k: v for k, v in matched.items() if _PROBE not in v}
    return {} if overlaps(out_pattern, need_pattern) else None


def _specificity(bindings: dict[str, str]) -> float:
    """Lower is more specific: total bound text (make's stem length).
    Unprovable relations ({}) are least specific of all."""
    if not bindings:
        return float("inf")
    return float(sum(len(v) for v in bindings.values()))


# -- collection membership (static) ------------------------------------------
# Selection can extract bindings that satisfy an action's *output template*
# while naming something the action's *collection* would never bind — e.g. a
# nested video path binds a render rule's `{video}` to a bare container name
# that the scene regex refuses. `_specificity` then picks that collection-blind
# candidate over the right one, and the wrong artifact gets cached over the
# right one. So before ranking, drop any candidate whose bindings a collection
# in its expansion spine *provably* refuses. This is purely static (pattern
# math, no workspace): membership against the real filesystem is the executor's
# job, and resolving it here would violate the selection/planner layering. The
# check is one-sided — it only rejects on a *proof* of non-membership; anything
# it cannot decide keeps the candidate, preserving selection's conservatism.

_REGEX_META = set(".^$*+?{}[]()|")


def _literal_of(pattern: str) -> str | None:
    """The single concrete string a fully-literal regex matches (collapsing
    ``\\x`` escapes), or None if any unescaped regex metacharacter remains —
    in which case we cannot reduce it to one probe path."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                return None
            out.append(pattern[i + 1])
            i += 2
            continue
        if c in _REGEX_META:
            return None
        out.append(c)
        i += 1
    return "".join(out)


def _regex_admits(coll: RegexData, bindings: dict[str, str]) -> bool | None:
    """True/False if provable that ``coll`` does/does not bind ``bindings``;
    None if it cannot be decided statically (keep the candidate)."""
    relevant = {k: v for k, v in bindings.items() if k in coll.binding_names}
    if not relevant:
        return None  # collection binds none of these names — nothing to prove
    filled, all_pinned = _fill_named_groups(coll.regex.pattern, relevant)
    if filled is None or not all_pinned:
        return None  # a group we could not pin — no single probe path exists
    path = _literal_of(filled)
    if path is None:
        return None  # residual regex structure — do not guess a path
    matched = coll.match(path)
    if matched is None:
        return False  # the collection regex refuses this exact path
    # It matches, but must reproduce the *same* values — the regex may bind a
    # name differently (a container name absorbed into a nested group).
    return None if any(matched.get(k) != v for k, v in relevant.items()) else True


def _collection_refuses(collection: DataExpr, bindings: dict[str, str]) -> bool:
    """A value is a member if *any* collection atom admits it; so refuse only
    when every atom provably rejects (and at least one is a checkable regex)."""
    verdicts = [
        _regex_admits(atom, bindings)
        for atom in collection.atoms()
        if isinstance(atom, RegexData)
    ]
    return bool(verdicts) and all(v is False for v in verdicts)


def _bindings_refused(action: Action, bindings: dict[str, str]) -> bool:
    """True if a collection anywhere in ``action``'s expansion spine provably
    refuses ``bindings`` — i.e. the target names a non-member and this
    candidate is collection-blind."""
    current: Action = action
    while isinstance(current, ExpandAll):
        if _collection_refuses(current.collection, bindings):
            return True
        current = current.child
    return False


def select_actions_with_origins(
    actions: list[Action], targets: list[str], forced_orders=()
) -> list[tuple[int, Action]]:
    """The subset (possibly binding-constrained) of ``actions`` needed to
    produce ``targets``, in original graph order."""
    from .planner import input_atoms, output_atoms

    queue: list = [FileData(t) for t in targets]
    seen_needs = {repr(n) for n in queue}
    selected: dict[tuple[int, tuple], Action] = {}

    def select(index: int, bindings: dict[str, str]) -> None:
        key = (index, tuple(sorted(bindings.items())))
        if key in selected:
            return
        constrained = actions[index]
        if bindings and isinstance(constrained, Expandable):
            constrained = constrained.substitute(bindings)
        selected[key] = constrained
        for atom in input_atoms(constrained):
            if isinstance(atom, (FileData, GlobData, RegexData)):
                marker = repr(atom)
                if marker not in seen_needs:
                    seen_needs.add(marker)
                    queue.append(atom)
        for before, after, _trigger in forced_orders:
            if after == index:
                select(before, {})

    while queue:
        need = queue.pop(0)
        candidates: list[tuple[int, dict[str, str]]] = []
        for index, action in enumerate(actions):
            for out_atom in output_atoms(action):
                bindings = _extract_bindings(out_atom, need)
                if bindings is not None:
                    candidates.append((index, bindings))
        if not candidates:
            continue  # a source file, not something an action produces
        # Drop collection-blind candidates: a match whose bindings a nested
        # expansion's collection provably refuses is not a real producer of
        # this target, and must not be allowed to win on `_specificity`.
        candidates = [
            (index, bindings)
            for index, bindings in candidates
            if not _bindings_refused(actions[index], bindings)
        ]
        if not candidates:
            continue
        best = min(_specificity(b) for _, b in candidates)
        for index, bindings in candidates:
            if _specificity(bindings) > best:
                continue  # shortest stem wins
            select(index, bindings)

    if not selected:
        raise ValueError(f"no action produces any of: {', '.join(targets)}")
    return [(key[0], selected[key]) for key in sorted(selected)]


def select_actions(actions: list[Action], targets: list[str]) -> list[Action]:
    return [action for _, action in select_actions_with_origins(actions, targets)]
