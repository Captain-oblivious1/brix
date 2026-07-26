/**
 * Reporters and the event adapter — the TS mirror of `src/brix/report.py` plus
 * `engine._EventAdapter`.
 *
 * Reporters are plain objects with optional methods (sanctioned deviation #3):
 * no subclassing. The event adapter parses each event JSON string the native
 * `build` delivers (live, on the JS loop) and replays it onto the reporter with
 * camelCased arguments.
 */

import {
  BuildResult,
  NodeRef,
  NodeState,
  ProcessResult,
  decodeProcessResult,
} from "./results";

export interface Reporter {
  buildBegin?(plan: { nodes: NodeRef[] }): void;
  nodeStarted?(node: NodeRef): void;
  processStarted?(node: NodeRef): void;
  nodeCompleted?(
    node: NodeRef,
    status: NodeState,
    result: ProcessResult | null,
    error: string | null,
  ): void;
  nodeSkipped?(node: NodeRef, why: string): void;
  collectionRealized?(node: NodeRef, members: string[], children: NodeRef[]): void;
  nodeInserted?(node: NodeRef, reason: string): void;
  buildEnd?(result: BuildResult): void;
}

/** Parse one event JSON string and dispatch it onto a reporter. */
export function dispatchEvent(reporter: Reporter, eventJson: string): void {
  const e = JSON.parse(eventJson);
  switch (e.event) {
    case "build_begin":
      reporter.buildBegin?.({ nodes: e.plan.nodes });
      break;
    case "node_started":
      reporter.nodeStarted?.(e.node);
      break;
    case "process_started":
      reporter.processStarted?.(e.node);
      break;
    case "node_completed":
      reporter.nodeCompleted?.(
        e.node,
        e.status,
        e.result ? decodeProcessResult(e.result) : null,
        e.error ?? null,
      );
      break;
    case "node_skipped":
      reporter.nodeSkipped?.(e.node, e.why);
      break;
    case "collection_realized":
      reporter.collectionRealized?.(e.node, e.members, e.children);
      break;
    case "node_inserted":
      reporter.nodeInserted?.(e.node, e.reason);
      break;
    // build_end is synthesized from the returned result (it carries the final
    // plan the event stream's copy would not), exactly as the Python skin does.
  }
}

/** A minimal console reporter: one line per node outcome. */
export class ConsoleReporter implements Reporter {
  constructor(private readonly showReplays = true) {}

  nodeCompleted(
    node: NodeRef,
    status: NodeState,
    result: ProcessResult | null,
    error: string | null,
  ): void {
    if (status === "skipped") return;
    const replayed = result != null && result.source !== "process";
    if (replayed && !this.showReplays) return;
    const mark = status === "done" ? "✓" : "✗";
    let line = `${mark} ${node.label}`;
    if (replayed) line += ` (${result!.source})`;
    if (error) line += `\n  ${error}`;
    console.log(line);
  }

  nodeSkipped(node: NodeRef, why: string): void {
    console.log(`· ${node.label} (${why})`);
  }
}
