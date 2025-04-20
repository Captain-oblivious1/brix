import os
import shutil
from brix.core import execute_dependency_graph
from tests.cpp.simple.build import file_loader, app_exe, lib_so

def test_directory_creation(tmp_path):
    # Get absolute path to tests/cpp/simple
    test_dir = os.path.join(os.path.dirname(__file__), "cpp", "simple")
    
    # Copy the test project to a temporary directory
    project_dir = tmp_path / "simple"
    shutil.copytree(test_dir, project_dir)
    os.chdir(project_dir)

    # Ensure build directory doesn't exist
    shutil.rmtree("build", ignore_errors=True)

    # Run the build
    execute_dependency_graph(app_exe, lib_so, n_threads=1)

    # Verify directories exist
    assert os.path.isdir("build")
    assert os.path.isdir("build/obj")
    assert os.path.isdir("build/obj/myLib")
    assert os.path.isdir("build/bin")

    # Verify output files exist
    assert os.path.exists("build/obj/myLib/lib.o")
    assert os.path.exists("build/obj/app.o")
    assert os.path.exists("build/bin/libexample.so")
    assert os.path.exists("build/bin/app")
