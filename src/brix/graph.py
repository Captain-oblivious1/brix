"""The logical graph: the user's clean-build truth."""

from __future__ import annotations

from .actions import Action
from .data import DataFactory


class Graph:
    """A logical build graph.

    Holds action declarations. The engine normally infers dependencies from
    data-set intersections; ``force_order`` covers the exceptional case where
    actions must be ordered without sharing data.
    """

    def __init__(self) -> None:
        self.data = DataFactory()
        self.actions: list[Action] = []
        self.forced_orders: list[tuple[int, int, object]] = []

    def add(self, *actions: Action) -> Graph:
        stack = list(reversed(actions))
        while stack:
            action = stack.pop()
            if not isinstance(action, Action):
                raise TypeError(f"Graph.add expects Action instances, got {action!r}")
            lower = getattr(action, "lower", None)
            if lower is not None:
                # Typed helpers (brix.cpp, …) lower into the generic model
                # here — one underlying representation for everything.
                stack.extend(reversed(lower()))
            else:
                self.actions.append(action)
        return self

    def force_order(self, before: Action, after: Action, *, trigger="on-success") -> Graph:
        """Require ``before`` to finish before ``after`` may run.

        This is for ordering with no data relationship; ordinary file/data
        dependencies should remain implicit. Both actions must already have
        been added to this graph exactly once.
        """
        from .plan import EdgeTrigger

        try:
            edge_trigger = trigger if isinstance(trigger, EdgeTrigger) else EdgeTrigger(trigger)
        except (TypeError, ValueError):
            raise ValueError("trigger must be 'on-success' or 'on-completion'") from None
        positions = []
        for action in (before, after):
            found = [i for i, candidate in enumerate(self.actions) if candidate is action]
            if len(found) != 1:
                raise ValueError("force_order actions must each be added to this graph exactly once")
            positions.append(found[0])
        if positions[0] == positions[1]:
            raise ValueError("force_order requires two different actions")
        order = (positions[0], positions[1], edge_trigger)
        existing = [o for o in self.forced_orders if o[:2] == order[:2]]
        if existing and existing[0][2] is not edge_trigger:
            raise ValueError("force_order already exists with a different trigger")
        if not existing:
            self.forced_orders.append(order)
        return self
