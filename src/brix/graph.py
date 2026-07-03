"""The logical graph: the user's clean-build truth."""

from __future__ import annotations

from .actions import Action
from .data import DataFactory


class Graph:
    """A logical build graph.

    Holds action declarations. Dependency relationships are not declared;
    the engine infers them from data-set intersections during planning.
    """

    def __init__(self) -> None:
        self.data = DataFactory()
        self.actions: list[Action] = []

    def add(self, *actions: Action) -> Graph:
        for action in actions:
            if not isinstance(action, Action):
                raise TypeError(f"Graph.add expects Action instances, got {action!r}")
            self.actions.append(action)
        return self
