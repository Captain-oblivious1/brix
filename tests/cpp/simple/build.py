from brix.core import File, FileLoader, Command, CompileCppAction, LinkCppSharedAction, LinkCppAppAction, MakeDirAction, ExecuteOnTouchedAction, BuildFailure, execute_dependency_graph
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
make_dir_action = MakeDirAction(file_loader)

# Create Command nodes with ExecuteOnTouchedAction wrapping actions
make_build_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
make_obj_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
make_bin_dir = Command(action=ExecuteOnTouchedAction(make_dir_action, file_loader))
compile_lib = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
compile_app = Command(action=ExecuteOnTouchedAction(cpp_action, file_loader))
link_lib = Command(action=ExecuteOnTouchedAction(shared_link_action, file_loader))
link_app = Command(action=ExecuteOnTouchedAction(app_link_action, file_loader))

# Set dependencies using add_predecessors (Command -> File or File -> Command)
# Directory dependencies
build_dir.add_predecessors(make_build_dir)  # make_build_dir has no predecessors
make_obj_dir.add_predecessors(build_dir)    # obj_dir depends on build_dir
obj_dir.add_predecessors(make_obj_dir)
make_bin_dir.add_predecessors(build_dir)    # bin_dir depends on build_dir
bin_dir.add_predecessors(make_bin_dir)
obj_mylib_dir.add_predecessors(make_obj_dir)  # obj_mylib_dir depends on obj_dir

# Source and output file dependencies
compile_lib.add_predecessors(lib_cpp, lib_h, obj_mylib_dir)
lib_o.add_predecessors(compile_lib)
compile_app.add_predecessors(app_cpp, lib_h, obj_dir)
app_o.add_predecessors(compile_app)
link_lib.add_predecessors(lib_o, bin_dir)
lib_so.add_predecessors(link_lib)
link_app.add_predecessors(app_o, lib_so, bin_dir)
app_exe.add_predecessors(link_app)

# Execute the graph
if __name__ == "__main__":
    try:
        execute_dependency_graph(app_exe, lib_so, n_threads=4)
    except BuildFailure as e:
        print(f"Build terminated: {e}")
