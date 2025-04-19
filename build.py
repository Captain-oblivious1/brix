from brix.core import File, Command, execute_dependency_graph  # Changed: Import File instead of Data

# Changed: Updated action to avoid using name
def compile_action(self, predecessors, successors):
    print(f"Running command with {len(predecessors)} predecessors and {len(successors)} successors")

# Create example targets
src = File("src/main.c")  # Changed: Use File instead of Data, removed name, default attributes
obj = File("build/main.o")  # Changed: Use File instead of Data, removed name
exe = File("build/hello")  # Changed: Use File instead of Data, removed name
compile_cmd = Command(compile_action)  # Changed: Use Command without name
link_cmd = Command(compile_action)  # Changed: Use Command without name

# Set dependencies (alternating File -> Command -> File)
src.successors.add(compile_cmd)
compile_cmd.predecessors.add(src)
compile_cmd.successors.add(obj)
obj.predecessors.add(compile_cmd)
obj.successors.add(link_cmd)
link_cmd.predecessors.add(obj)
link_cmd.successors.add(exe)
exe.predecessors.add(link_cmd)

# Execute the graph
if __name__ == "__main__":
    execute_dependency_graph(exe, n_threads=4)  # Unchanged
