/**
 * Skin oracle for the `ts.npm` resolver: resolving a fixture node_modules tree
 * (own types, nested-node_modules skip, sorting, @types fallback, scoped-name
 * mangling, first-wins dedup) and threading the resulting DepSet through
 * ts.CompileAction(deps=...) lowers to the same wire as brix.ts.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, data as d, graphToSpec, ts } from "../index";

const FIXTURE = path.join(__dirname, "..", "..", "oracle", "fixtures", "npm");

function referenceGraph(): Graph {
  const g = new Graph();
  const deps = ts.npm(["foo", "bar", "@scope/thing", "foo"], { root: FIXTURE });
  g.add(
    new ts.CompileAction({
      sources: d.glob("src/{name}.ts"),
      outDir: "dist",
      dependencies: [d.file("build/upstream/mod.d.ts")],
      deps,
      trackImports: false,
    }),
  );
  return g;
}

test("TS ts.npm resolves + threads through CompileAction identically to Python", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_npm_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
