/**
 * Skin oracle for the cpp skin: CompileSourceAction (-MMD + toolchain env-used
 * snapshot), LinkApplicationAction, ArchiveAction, both RunTestAction binary-ref
 * branches, and DoxygenAction lower to the same wire as brix.cpp.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, cpp, data as d, graphToSpec } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new cpp.CompileSourceAction({
      source_file: d.glob("src/{name}.cpp"),
      object_file: d.glob("build/obj/{name}.o"),
      compiler: d.tool("clang++"),
      compile_options: ["-O2", "-std=c++20"],
    }),
    new cpp.LinkApplicationAction({
      object_files: d.glob("build/obj/{name}.o"),
      output_file: d.file("build/bin/app"),
      linker: d.tool("clang++"),
      link_options: ["-flto"],
    }),
    new cpp.ArchiveAction({
      object_files: d.glob("build/obj/{name}.o"),
      output_file: d.file("build/lib/libfoo.a"),
    }),
    new cpp.RunTestAction({ test_binary: "build/bin/test_foo", args: ["--gtest_color=no"] }),
    new cpp.RunTestAction({ test_binary: "bare_test", name: "run bare" }),
    new cpp.DoxygenAction({
      config: "Doxyfile",
      source_files: d.glob("src/{name}.h"),
      output_dir: "build/docs/html",
    }),
  );
  return g;
}

test("TS cpp skin lowers to the same spec as the Python cpp skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_cpp_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
