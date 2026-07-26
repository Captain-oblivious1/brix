/**
 * Skin oracle for the ts skin: CompileAction (with a ts-dts-lensed dependency —
 * proving the content-key crossing), RunTestAction, TypedocAction lower to the
 * same wire as brix.ts. (The import-tracking wrapper path is a pending decision,
 * so track_imports is off here — see ts.ts.)
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, data as d, graphToSpec, ts } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new ts.CompileAction({
      sources: d.glob("src/{name}.ts"),
      outDir: "dist",
      options: ["--target", "es2022", "--strict"],
      dependencies: [d.glob("../shared/dist/{name}.d.ts")],
      trackImports: false,
    }),
    new ts.RunTestAction({
      testFile: "tests/foo.test.ts",
      sources: ["src/foo.ts", d.glob("src/util/{name}.ts")],
      args: ["--reporter", "dot"],
    }),
    new ts.TypedocAction({
      entryPoints: d.glob("src/{name}.ts"),
      outputDir: "build/docs",
    }),
  );
  return g;
}

test("TS ts skin lowers to the same spec as the Python ts skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_ts_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
