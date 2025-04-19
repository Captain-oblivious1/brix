import pytest
from brix.core import Node, execute_dependency_graph

class NodeForTest(Node):  # Changed: Renamed from TestNode to avoid PytestCollectionWarning
    def __init__(self, name, action=None):
        super().__init__()
        self.name = name
        if action:
            self.action = action
    
    def __str__(self):
        return f"NodeForTest({self.name})"  # Changed: Updated to reflect new class name

def test_execute_dependency_graph():
    executed = []
    def action(self, predecessors, successors):
        executed.append(self.name)
    
    t1 = NodeForTest("t1", action)  # Changed: Updated from TestNode to NodeForTest
    t2 = NodeForTest("t2", action)  # Changed: Updated from TestNode to NodeForTest
    t3 = NodeForTest("t3", action)  # Changed: Updated from TestNode to NodeForTest
    t1.successors.add(t3)
    t2.successors.add(t3)
    t3.predecessors.add(t1)
    t3.predecessors.add(t2)
    
    execute_dependency_graph(t3, n_threads=2)
    assert set(executed) == {"t1", "t2", "t3"}
    assert executed.index("t3") > executed.index("t1")
    assert executed.index("t3") > executed.index("t2")

def test_cycle_detection():
    t1 = NodeForTest("t1", lambda self, p, s: None)  # Changed: Updated from TestNode to NodeForTest
    t2 = NodeForTest("t2", lambda self, p, s: None)  # Changed: Updated from TestNode to NodeForTest
    t1.successors.add(t2)
    t2.predecessors.add(t1)
    t2.successors.add(t1)
    t1.predecessors.add(t2)
    
    with pytest.raises(RuntimeError, match="Cycle detected"):
        execute_dependency_graph(t1, n_threads=2)

def test_node_without_action():
    executed = []
    def action(self, predecessors, successors):
        executed.append(self.name)
    
    t1 = NodeForTest("t1", action)  # Changed: Updated from TestNode to NodeForTest
    t2 = NodeForTest("t2")  # Changed: Updated from TestNode to NodeForTest
    t1.successors.add(t2)
    t2.predecessors.add(t1)
    
    execute_dependency_graph(t2, n_threads=2)
    assert executed == ["t1"]  # Only t1's action should execute
