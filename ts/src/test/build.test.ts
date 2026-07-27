/**
 * End-to-end: author a graph with the TS API, build it through the Rust engine,
 * and check the same behaviours the binding smoke proved — real run, then
 * incremental replay, then status, then clean — plus live events reaching a
 * reporter.
 */

import assert from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  ExternalProcessAction,
  Graph,
  IncrementalBuilder,
  Reporter,
  runClean,
  runStatus,
} from "../index";

function helloGraph(): Graph {
  const g = new Graph();
  const d = g.data;
  g.add(
    new ExternalProcessAction({
      name: "hello",
      commandLine: "{tool} {out}",
      inputs: { tool: d.tool("touch") },
      outputs: { out: d.file("hello.txt") },
    }),
  );
  return g;
}

test("end-to-end build, replay, status, clean, and live events", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "brix-ts-"));
  const g = helloGraph();
  const builder = new IncrementalBuilder(() => g, { root, metadataDir: "build/.brix" });

  const events: string[] = [];
  const reporter: Reporter = {
    nodeStarted: () => events.push("started"),
    nodeCompleted: () => events.push("completed"),
    buildEnd: () => events.push("end"),
  };

  const r1 = await builder.build({ workerIdleTimeout: 0, reporter });
  assert.strictEqual(r1.success, true);
  assert.ok(fs.existsSync(path.join(root, "hello.txt")), "output created");
  assert.deepStrictEqual(
    r1.processResults.map((p) => p.source),
    ["process"],
    "first build runs the process",
  );
  // Live events reached the reporter, and buildEnd was synthesized last.
  assert.deepStrictEqual(events, ["started", "completed", "end"]);

  const r2 = await builder.build({ workerIdleTimeout: 0 });
  assert.deepStrictEqual(
    r2.processResults.map((p) => p.source),
    ["replayed"],
    "second build replays incrementally",
  );

  const status = runStatus(g, {
    root,
    metadataDir: "build/.brix",
    stagingDir: "build/staging",
  });
  assert.strictEqual(status.actions[0].state, "fresh");

  const removed = runClean(g, { root, metadataDir: "build/.brix" });
  assert.ok(removed >= 1, "clean removed the output");
  assert.ok(!fs.existsSync(path.join(root, "hello.txt")), "output gone after clean");

  fs.rmSync(root, { recursive: true, force: true });
});

test("a throwing reporter is re-raised after the build, not crashing the process", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "brix-ts-"));
  const g = helloGraph();
  const builder = new IncrementalBuilder(() => g, { root, metadataDir: "build/.brix" });
  const boom: Reporter = {
    nodeCompleted: () => {
      throw new Error("reporter boom");
    },
  };

  let threw: Error | undefined;
  await builder.build({ workerIdleTimeout: 0, reporter: boom }).catch((e) => {
    threw = e as Error;
  });

  // The build ran to completion; the reporter error surfaced afterward (Python
  // contract) rather than crashing the process as an uncaught exception.
  assert.ok(threw && /reporter boom/.test(threw.message), "reporter error re-raised");
  assert.ok(
    fs.existsSync(path.join(root, "hello.txt")),
    "build completed despite the throwing reporter",
  );
  fs.rmSync(root, { recursive: true, force: true });
});
