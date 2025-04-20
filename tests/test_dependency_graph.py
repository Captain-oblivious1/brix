import pytest
from brix.core import File, Command, execute_dependency_graph, Status

def test_execute_dependency_graph():
    # Create a simple graph: File -> Command -> File
    input_file = File("input.txt", hash="abc", status=Status.CREATED)
    output_file = File("output.txt", hash="", status=Status.DELETED)
    command = Command()

    input_file.add_predecessors(command)
    output_file.add_predecessors(command)

    execute_dependency_graph(output_file, n_threads=1)

    # No assertions needed; test passes if no errors

def test_cycle_detection():
    # Create a cyclic graph
    file1 = File("file1.txt")
    file2 = File("file2.txt")
    cmd1 = Command()
    cmd2 = Command()

    file1.add_predecessors(cmd1)
    cmd2.add_predecessors(file1)
    file2.add_predecessors(cmd2)
    cmd1.add_predecessors(file2)  # Creates cycle

    with pytest.raises(RuntimeError, match=r"Cycle detected involving"):
        execute_dependency_graph(file2, n_threads=1)

def test_node_without_action():
    # Create a graph with a Command node without an action
    input_file = File("input.txt", hash="abc", status=Status.CREATED)
    output_file = File("output.txt", hash="", status=Status.DELETED)
    command = Command()  # No action

    input_file.add_predecessors(command)
    output_file.add_predecessors(command)

    execute_dependency_graph(output_file, n_threads=1)
