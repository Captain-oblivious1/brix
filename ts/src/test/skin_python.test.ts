/**
 * Skin oracle: the TS `python.PytestAction` must lower to exactly the core
 * actions the Python `python.PytestAction` does. Same reusable harness as the
 * core oracle — author the same usage in both languages, lower, deep-equal.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, data as d, graphToSpec, python } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new python.PytestAction({
      testSources: d.glob("tests/test_{name}.py"),
      python: d.tool("python3"),
      manifestDir: "build/deps",
      options: ["-q", "-x"],
      env: { PYTHONPATH: "src" },
    }),
  );
  return g;
}

test("TS python skin lowers to the same spec as the Python python skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_python_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
