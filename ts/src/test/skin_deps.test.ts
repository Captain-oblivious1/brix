/**
 * Skin oracle for the shared `DepSet`: composition (`.plus` ordered merge with
 * first-wins dedup, `.excluding` fnmatch exclude), the compile/link flag split,
 * and both consumers — cpp `deps=` and kotlin `classpath=` — lower to the same
 * wire as the Python DepSet threaded through brix.cpp / brix.kotlin.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { DepSet, Graph, cpp, data as d, graphToSpec, kotlin } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  const cdeps = new DepSet(
    ["build/lib/libfoo.a"],
    ["-I/opt/foo/include", "-DFOO=1"],
    ["-L/opt/foo/lib", "-lfoo"],
  )
    .plus(new DepSet(["build/lib/libbar.a", "build/lib/libfoo.a"], [], ["-lbar"]))
    .excluding("*/libbar.a");
  const cp = new DepSet(["libs/guava.jar", "libs/annotations.jar"]);
  g.add(
    new cpp.CompileSourceAction({
      source_file: d.glob("src/{name}.cpp"),
      object_file: d.glob("build/obj/{name}.o"),
      compiler: d.tool("clang++"),
      compile_options: ["-O2"],
      deps: cdeps,
    }),
    new cpp.LinkApplicationAction({
      object_files: d.glob("build/obj/{name}.o"),
      output_file: d.file("build/bin/app"),
      linker: d.tool("clang++"),
      deps: cdeps,
    }),
    new kotlin.JarAction({
      sourceFiles: d.glob("src/main/kotlin/{name}.kt"),
      outputFile: d.file("build/libs/app.jar"),
      classpath: cp,
    }),
  );
  return g;
}

test("TS DepSet threads through cpp deps= and kotlin classpath= identically", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_deps_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
