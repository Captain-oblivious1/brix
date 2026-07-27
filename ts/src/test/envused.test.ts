/**
 * Prove the env_used snapshot (`_brix_env_used`) byte-matches Python's
 * json.dumps for real values — a set variable (with an embedded quote and a
 * non-ASCII char, to stress escaping), a set-empty variable, and an unset one.
 * Both processes read the same environment (the python3 subprocess inherits
 * this process's env), so a match proves the JSON encoding agrees.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { ExternalProcessAction, Graph, data as d, graphToSpec } from "../index";

test("env_used snapshot byte-matches Python for set / empty / unset vars", () => {
  // Set on this process's env so the TS action and the python3 subprocess
  // (which inherits it) both read identical values.
  process.env.BRIX_TEST_SET = 'a "quote" and é';
  process.env.BRIX_TEST_EMPTY = "";
  delete process.env.BRIX_TEST_UNSET;

  const g = new Graph();
  g.add(
    new ExternalProcessAction({
      name: "probe",
      commandLine: "{t}",
      inputs: { t: d.tool("true") },
      envUsed: ["BRIX_TEST_SET", "BRIX_TEST_EMPTY", "BRIX_TEST_UNSET"],
    }),
  );
  const tsSpec = graphToSpec(g);

  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_envused.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));

  assert.deepStrictEqual(tsSpec, pySpec);

  delete process.env.BRIX_TEST_SET;
  delete process.env.BRIX_TEST_EMPTY;
});
