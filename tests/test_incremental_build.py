import os
import shutil
import json
import pytest
from brix.core import FileLoader, Status, execute_dependency_graph
from tests.cpp.simple.build import file_loader, app_exe, lib_so

def test_incremental_build(tmp_path):
    # Get absolute path to tests/cpp/simple
    test_dir = os.path.join(os.path.dirname(__file__), "cpp", "simple")
    
    # Copy the test project to a  temporary directory
    project_dir = tmp_path / "simple"
    shutil.copytree(test_dir, project_dir)
    os.chdir(project_dir)

    # Run the first build
    execute_dependency_graph(app_exe, lib_so, n_threads=1)

    # Verify output files exist
    assert os.path.exists("build/obj/myLib/lib.o")
    assert os.path.exists("build/obj/app.o")
    assert os.path.exists("build/bin/libexample.so")
    assert os.path.exists("build/bin/app")

    # Check cache file
    with open("build/.brix_cache.json", "r") as f:
        cache = json.load(f)
    assert "build/obj/myLib/lib.o" in cache
    assert "build/obj/app.o" in cache
    assert "build/bin/libexample.so" in cache
    assert "build/bin/app" in cache
    assert all(cache[path] != "" for path in cache if not path.startswith("build/") and not path.endswith((".cpp", ".h")))

    # Capture initial hashes
    initial_hashes = {
        "lib.o": file_loader.load_file("build/obj/myLib/lib.o").hash,
        "app.o": file_loader.load_file("build/obj/app.o").hash,
        "lib.so": file_loader.load_file("build/bin/libexample.so").hash,
        "app": file_loader.load_file("build/bin/app").hash,
    }

    # Run the second build (no changes)
    execute_dependency_graph(app_exe, lib_so, n_threads=1)

    # Verify file statuses are UNCHANGED
    assert file_loader.load_file("build/obj/myLib/lib.o").status == Status.UNCHANGED
    assert file_loader.load_file("build/obj/app.o").status == Status.UNCHANGED
    assert file_loader.load_file("build/bin/libexample.so").status == Status.UNCHANGED
    assert file_loader.load_file("build/bin/app").status == Status.UNCHANGED

    # Verify hashes haven't changed
    for path, initial_hash in initial_hashes.items():
        current_hash = file_loader.load_file(f"build/{path.replace('.', '/')}" if '.' in path else path).hash
        assert current_hash == initial_hash, f"Hash for {path} changed unexpectedly"
