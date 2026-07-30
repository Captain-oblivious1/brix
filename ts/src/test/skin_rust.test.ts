/** Skin oracle for CargoBuildAction, including flags and environment tracking. */

import assert from "node:assert";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test } from "node:test";

import { Graph, data as d, graphToSpec, rust } from "../index";

function referenceGraph(): Graph {
  const g = new Graph();
  g.add(
    new rust.CargoBuildAction({
      manifest: "crates/widget/Cargo.toml",
      artifact: d.file("target/debug/libwidget.a"),
      sources: d.glob("crates/{name}.rs"),
      cargo: d.tool("cargo-nightly"),
      release: false,
      offline: true,
      options: ["--locked", "--features", "ffi"],
      env: { RUSTFLAGS: "-C debuginfo=1", WIDGET_MODE: "oracle" },
      envUsed: ["CI"],
    }),
    // Bare defaults: release=true (the --release branch), no offline, no
    // options/env/envUsed, default cargo tool — the otherwise-uncovered path.
    new rust.CargoBuildAction({
      manifest: "crates/bare/Cargo.toml",
      artifact: d.file("target/release/libbare.a"),
      sources: d.glob("crates/bare/{name}.rs"),
    }),
  );
  return g;
}

test("TS rust skin lowers to the same spec as the Python rust skin", () => {
  const tsSpec = graphToSpec(referenceGraph());
  const pyScript = path.join(__dirname, "..", "..", "oracle", "py_rust_skin.py");
  const pySpec = JSON.parse(execFileSync("python3", [pyScript], { encoding: "utf8" }));
  assert.deepStrictEqual(tsSpec, pySpec);
});
