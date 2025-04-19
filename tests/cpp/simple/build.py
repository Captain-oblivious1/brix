from brix.core import File, FileLoader, Command, CompileCppAction, LinkCppSharedAction, LinkCppAppAction, ExecuteOnTouchedAction, BuildFailure, execute_dependency_graph
import os

# Use directory path for source files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # tests/cpp/simple/

# Create build directory relative to tests/cpp/simple/
os.makedirs(os.path.join(BASE_DIR, "build"), exist_ok=True)

# Define cache file and initialize FileLoader
CACHE_FILE = os.path.join(BASE_DIR, "build/.brix_cache.json")
file_loader = FileLoader(CACHE_FILE, BASE_DIR)

# Create File nodes using FileLoader
lib_cpp = file_loader.load_file(os.path.join(BASE_DIR, "lib.cpp"))
lib_h = file_loader.load_file(os.path.join(BASE_DIR, "lib.h"))
app_cpp = file_loader.load_file(os.path.join(BASE_DIR, "app.cpp"))
lib_o = file_loader.load_file(os.path.join(BASE_DIR, "build/lib.o"))
app_o = file_loader.load_file(os.path.join(BASE_DIR, "build/app.o"))
lib_so = file_loader.load_file(os.path.join(BASE_DIR, "build/libexample.so"))
app_exe = file_loader.load_file(os.path.join(BASE_DIR, "build/app"))

# Create single action instances
cpp_action = CompileCppAction()
shared_link_action = LinkCppSharedAction()
app_link_action = LinkCppAppAction()

# Create Command nodes with ExecuteOnTouchedAction wrapping actions
compile_lib = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
compile_app = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
link_lib = Command(action=ExecuteOnTouchedAction(shared_link_action, file_loader))
link_app = Command(action=ExecuteOnTouchedAction(app_link_action, file_loader))

# Set dependencies (File -> Command -> File)
lib_cpp.successors.add(compile_lib)
lib_h.successors.add(compile_lib)  # Header as dependency
compile_lib.predecessors.add(lib_cpp)
compile_lib.predecessors.add(lib_h)
compile_lib.successors.add(lib_o)
lib_o.predecessors.add(compile_lib)

app_cpp.successors.add(compile_app)
lib_h.successors.add(compile_app)  # Header for app
compile_app.predecessors.add(app_cpp)
compile_app.predecessors.add(lib_h)
compile_app.successors.add(app_o)
app_o.predecessors.add(compile_app)

lib_o.successors.add(link_lib)
link_lib.predecessors.add(lib_o)
link_lib.successors.add(lib_so)
lib_so.predecessors.add(link_lib)

app_o.successors.add(link_app)
lib_so.successors.add(link_app)  # Library as dependency
link_app.predecessors.add(app_o)
link_app.predecessors.add(lib_so)
link_app.successors.add(app_exe)
app_exe.predecessors.add(link_app)

# Execute the graph
if __name__ == "__main__":
    try:
        execute_dependency_graph(app_exe, lib_so, n_threads=4)
    except BuildFailure as e:
        print(f"Build terminated: {e}")
