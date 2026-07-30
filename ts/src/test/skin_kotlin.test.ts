/** Skin oracle for spawned kotlinc, jar packaging, and the kotlin-abi lens. */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import {
  ExternalProcessAction,
  Graph,
  data as d,
  graphToSpec,
  kotlin,
} from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new kotlin.JarAction({
      sourceFiles: d.glob("src/main/kotlin/{name}.kt"),
      outputFile: d.file("build/libs/app.jar"),
      kotlinc: d.tool("kotlinc-custom"),
      jarTool: d.tool("jar-custom"),
      classesDir: "build/classes/kotlin",
      jvmTarget: "21",
      springAllopen: "tools/allopen-compiler-plugin.jar",
      options: ["-Xjsr305=strict"],
      entryPoint: "com.acme.AppKt",
      resourceDir: "src/main/resources",
    }),
    new ExternalProcessAction({
      name: "consume Kotlin ABI",
      commandLine: "{tool} {classpath}",
      inputs: {
        tool: d.tool("consumer"),
        classpath: kotlin.abi(
          d.file("build/libs/app.jar").plus(d.glob("libs/{name}.jar")),
        ),
      },
      outputs: { output: d.file("build/consumed.txt") },
    }),
    // Bare defaults: no entryPoint (the `jar cf` branch), no resourceDir, no
    // springAllopen, default kotlinc/jar/classesDir/jvmTarget — the
    // otherwise-uncovered path.
    new kotlin.JarAction({
      sourceFiles: d.glob("lib/src/{name}.kt"),
      outputFile: d.file("build/libs/lib.jar"),
    }),
  );
  return g;
}

test("TS kotlin skin lowers to the same spec as the Python kotlin skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_kotlin_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
