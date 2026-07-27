/**
 * Oracle for the `asset` atom (a brix-shipped helper referenced by logical
 * name): both frontends must emit an identical asset reference. This is the
 * frontend half of the engine-owned, content-addressed asset mechanism
 * (docs/boundary-schema.md); the engine-side resolution + fingerprint is next.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { ExternalProcessAction, Graph, data as d, graphToSpec } from "../index";
import { AssetData } from "../data";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new ExternalProcessAction({
      name: "run worker",
      commandLine: "{java} -jar {worker}",
      inputs: { java: d.tool("java"), worker: new AssetData("java/JavacWorker.java") },
      outputs: {},
    }),
  );
  return g;
}

test("asset atom lowers identically in both frontends", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_asset.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
