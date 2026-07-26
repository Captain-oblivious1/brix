/**
 * Build result types and wire decoding — the TS mirror of the result half of
 * `src/brix/process.py` + `engine.py`. Wire shapes are snake_case
 * (docs/boundary-schema.md); the authoring/result surface is camelCase, mapped
 * here at the edge (sanctioned deviation #1).
 */

export type NodeState = "pending" | "running" | "done" | "failed" | "skipped";

export interface NodeRef {
  id: number;
  label: string;
  kind: "process" | "expand";
}

export interface ProcessResult {
  actionName: string;
  command: string;
  argv: string[];
  exitCode: number | null;
  signal: number | null;
  stdout: string;
  stderr: string;
  source: "process" | "replayed" | "cache";
}

export interface BuildResult {
  success: boolean;
  errors: string[];
  states: Record<number, NodeState>;
  nodeResults: Record<number, ProcessResult>;
  processResults: ProcessResult[];
  plan: { nodes: NodeRef[] };
}

export function decodeProcessResult(raw: Record<string, unknown>): ProcessResult {
  return {
    actionName: raw.action_name as string,
    command: raw.command as string,
    argv: raw.argv as string[],
    exitCode: (raw.exit_code as number | null) ?? null,
    signal: (raw.signal as number | null) ?? null,
    stdout: raw.stdout as string,
    stderr: raw.stderr as string,
    source: raw.source as ProcessResult["source"],
  };
}

export function decodeBuildResult(text: string): BuildResult {
  const raw = JSON.parse(text);
  const states: Record<number, NodeState> = {};
  for (const [id, st] of Object.entries(raw.states as Record<string, NodeState>)) {
    states[Number(id)] = st;
  }
  const nodeResults: Record<number, ProcessResult> = {};
  for (const [id, r] of Object.entries(
    raw.node_results as Record<string, Record<string, unknown>>,
  )) {
    nodeResults[Number(id)] = decodeProcessResult(r);
  }
  return {
    success: raw.success,
    errors: raw.errors,
    states,
    nodeResults,
    processResults: (raw.process_results as Record<string, unknown>[]).map(
      decodeProcessResult,
    ),
    plan: { nodes: raw.plan.nodes as NodeRef[] },
  };
}

/** A summary line, mirroring Python's `BuildResult.summary()`. */
export function summarize(result: BuildResult): string {
  const counts: Record<string, number> = {};
  for (const node of result.plan.nodes) {
    const st = result.states[node.id] ?? "pending";
    counts[st] = (counts[st] ?? 0) + 1;
  }
  const parts = Object.entries(counts)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([state, count]) => `${count} ${state}`);
  const head = result.success ? "BUILD OK" : "BUILD FAILED";
  return `${head} (${parts.join(", ")})`;
}
