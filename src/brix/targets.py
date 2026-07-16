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

from .actions import Action, Expandable
from .data import FileData, GlobData, RegexData
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
