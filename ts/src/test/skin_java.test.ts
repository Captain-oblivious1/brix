/**
 * Skin oracle for the java skin: JarAction (compile with the JVM env-used
 * snapshot + jar with entry point and resource dir) and JavadocAction with a
 * jvm-abi-lensed classpath lower to the same wire as brix.java. Covers both the
 * env-sentinel path (_brix_env_used must byte-match Python's json.dumps) and the
 * jvm-abi content-lens crossing.
 */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, data as d, graphToSpec, java } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new java.JarAction({
      sourceFiles: d.glob("src/main/java/{name}.java"),
      outputFile: d.file("build/libs/app.jar"),
      options: ["--release", "17"],
      classesDir: "build/classes",
      entryPoint: "com.acme.App",
      resourceDir: "src/main/resources",
    }),
    new java.JavadocAction({
      sourceFiles: d.glob("src/main/java/{name}.java"),
      outputDir: "build/docs",
      classpath: [d.glob("libs/{name}.jar")],
      options: ["-quiet"],
    }),
  );
  return g;
}

test("TS java skin lowers to the same spec as the Python java skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_java_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
