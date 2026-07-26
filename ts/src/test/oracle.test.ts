/**
 * The faithfulness oracle: the TS lowering must deep-equal the Python lowering
 * for the same logical graph. The graph is hand-mirrored from
 * `oracle/py_spec.py` — two independent authorings of one intent — and the wire
 * spec is what the Rust core actually runs, so equality here means the TS
 * authoring surface is faithful, not merely plausible.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import {
  ExpandAll,
  ExternalProcessAction,
  Graph,
  data as d,
  graphToSpec,
} from "../index";

function referenceGraph(): Graph {
  const g = new Graph();

  const inner = new ExternalProcessAction({
    name: "test {module}/{case}",
    commandLine: "{py} -m pytest {src} {opts}",
    inputs: {
      py: d.tool("python3"),
      src: d.file("tests/{module}/{case}.py"),
      opts: d.value("-q"),
      // a union: glob + single file
      extra: d.glob("fixtures/{module}/*.json").plus(d.file("conftest.py")),
    },
    outputs: { report: d.file("build/{module}/{case}.xml") },
    env: { PYTHONPATH: "src" },
    depfile: "build/{module}/{case}.d",
  });
  const tests = new ExpandAll({
    elementName: "module",
    collection: d.glob("tests/{module:*}"),
    child: new ExpandAll({
      elementName: "case",
      collection: d.regex("tests/(?P<module>[^/]+)/(?P<case>[^/]+)\\.py"),
      child: inner,
    }),
  });
  const compile = new ExternalProcessAction({
    name: "compile",
    commandLine: "{cc} -c {src} -o {obj}",
    inputs: {
      cc: d.tool("cc"),
      // a glob with an exclusion narrowing
      src: d.glob("src/*.c").excluding("*_test.c"),
    },
    outputs: { obj: d.file("build/all.o") },
  });
  g.add(compile, tests);
  g.forceOrder(compile, tests);
  return g;
}

test("TS lowering deep-equals Python lowering", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_spec.py");
  const pyJson = execFileSync("python3", [pyScript], { encoding: "utf8" });
  const pySpec = JSON.parse(pyJson);
  assert.deepStrictEqual(tsSpec, pySpec);
});
