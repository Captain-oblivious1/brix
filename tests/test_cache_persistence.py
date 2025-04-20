import os
import shutil
import json
import pytest
from brix.core import FileLoader, Status, execute_dependency_graph
from tests.cpp.simple.build import file_loader, app_exe, lib_so

def test_cache_persistence(tmp_path):
    # Get absolute path to tests/cpp/simple
    test_dir = os.path.join(os.path.dirname(__file__), "cpp", "simple")
    
    # Copy the test project to a temporary directory
    project_dir = tmp_path / "simple"
    shutil.copytree(test_dir, project_dir)
    os.chdir(project_dir)

    # Verify cache path
    cache_file = "build/.brix_cache.json"
    assert file_loader.cache_file == os.path.join(test_dir, cache_file), f"Expected cache file at {test_dir}/{cache_file}, got {file_loader.cache_file}"

    # Run the first build
    try:
        execute_dependency_graph(app_exe, lib_so, n_threads=1)
    except Exception as e:
        pytest.fail(f"Build failed: {e}")

    # Verify output files exist
    assert os.path.exists("build/obj/myLib/lib.o"), "lib.o not created"
    assert os.path.exists("build/obj/app.o"), "app.o not created"
    assert os.path.exists("build/bin/libexample.so"), "libexample.so not created"
    assert os.path.exists("build/bin/app"), "app not created"

    # Check cache file
    assert os.path.exists(cache_file), f"Cache file {cache_file} not created"
    with open(cache_file, "r") as f:
        cache = json.load(f)

    # Verify cache contains expected files
    expected_files = [
        "build/obj/myLib/lib.o",
        "build/obj/app.o",
        "build/bin/libexample.so",
        "build/bin/app",
        "build",
        "build/obj",
        "build/obj/myLib",
        "build/bin",
        "src/myLib/lib.cpp",
        "src/myLib/lib.h",
        "src/app.cpp",
    ]
    for path in expected_files:
        assert path in cache, f"Missing {path} in cache"

    # Create a new FileLoader to simulate a fresh run
    new_loader = FileLoader(cache_file, file_loader.root_dir)

    # Verify file statuses
    for path in expected_files:
        file = new_loader.load_file(path)
        if os.path.isdir(path) or not os.path.exists(path):
            assert file.hash == "", f"Non-empty hash for directory {path}"
        else:
            assert file.hash != "", f"Empty hash for file {path}"
            assert file.status == Status.UNCHANGED, f"File {path} not UNCHANGED (got {file.status})"
