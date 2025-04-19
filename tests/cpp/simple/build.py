from brix.core import File, FileLoader, Command, CompileCppAction, LinkCppSharedAction, LinkCppAppAction, MakeDir, ExecuteOnTouchedAction, BuildFailure, execute_dependency_graph
import os

# Use directory path for source files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # tests/cpp/simple/

# Define cache file and initialize FileLoader
CACHE_FILE = "build/.brix_cache.json"
file_loader = FileLoader(CACHE_FILE, BASE_DIR)

# Create File nodes for directories
build_dir = file_loader.load_file("build")
obj_dir = file_loader.load_file("build/obj")
obj_mylib_dir = file_loader.load_file("build/obj/myLib")
bin_dir = file_loader.load_file("build/bin")

# Create File nodes for source and output files
lib_cpp = file_loader.load_file("src/myLib/lib.cpp")
lib_h = file_loader.load_file("src/myLib/lib.h")
app_cpp = file_loader.load_file("src/app.cpp")
lib_o = file_loader.load_file("build/obj/myLib/lib.o")
app_o = file_loader.load_file("build/obj/app.o")
lib_so = file_loader.load_file("build/bin/libexample.so")
app_exe = file_loader.load_file("build/bin/app")

# Create single action instances with file_loader
cpp_action = CompileCppAction(file_loader)
shared_link_action = LinkCppSharedAction(file_loader)
app_link_action = LinkCppAppAction(file_loader)
make_dir_action = MakeDir(file_loader)

# Create Command nodes with ExecuteOnTouchedAction wrapping actions
make_build_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
make_obj_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
make_obj_mylib_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
make_bin_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
compile_lib = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
compile_app = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
link_lib = Command(action=ExecuteOnTouchedAction(shared_link_action, file_loader))
link_app = Command(action=ExecuteOnTouchedAction(app_link_action, file_loader))

# Set dependencies (File -> Command -> File)
# Directory dependencies
build_dir.successors.add(make_build_dir)
make_build_dir.predecessors.add(build_dir)
make_build_dir.successors.add(obj_dir)
make_build_dir.successors.add(bin_dir)
obj_dir.predecessors.add(make_build_dir)
obj_dir.successors.add(make_obj_dir)
make_obj_dir.predecessors.add(obj_dir)
make_obj_dir.successors.add(obj_mylib_dir)
obj_mylib_dir.predecessors.add(make_obj_dir)
obj_mylib_dir.successors.add(make_obj_mylib_dir)
make_obj_mylib_dir.predecessors.add(obj_mylib_dir)
bin_dir.predecessors.add(make_build_dir)
bin_dir.successors.add(make_bin_dir)
make_bin_dir.predecessors.add(bin_dir)

# Source and output file dependencies
lib_cpp.successors.add(compile_lib)
lib_h.successors.add(compile_lib)
compile_lib.predecessors.add(lib_cpp)
compile_lib.predecessors.add(lib_h)
compile_lib.predecessors.add(obj_mylib_dir)
compile_lib.successors.add(lib_o)
lib_o.predecessors.add(compile_lib)

app_cpp.successors.add(compile_app)
lib_h.successors.add(compile_app)
compile_app.predecessors.add(app_cpp)
compile_app.predecessors.add(lib_h)
compile_app.predecessors.add(obj_dir)
compile_app.successors.add(app_o)
app_o.predecessors.add(compile_app)

lib_o.successors.add(link_lib)
link_lib.predecessors.add(lib_o)
link_lib.predecessors.add(bin_dir)
link_lib.successors.add(lib_so)
lib_so.predecessors.add(link_lib)

app_o.successors.add(link_app)
lib_so.successors.add(link_app)
link_app.predecessors.add(app_o)
link_app.predecessors.add(lib_so)
link_app.predecessors.add(bin_dir)
link_app.successors.add(app_exe)
app_exe.predecessors.add(link_app)

# Execute the graph
if __name__ == "__main__":
    try:
        execute_dependency_graph(app_exe, lib_so, n_threads=4)
    except BuildFailure as e:
        print(f"Build terminated: {e}")
