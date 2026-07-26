/**
 * CLI helpers — the TS mirror of `src/brix/cli.py`. The command-line section of
 * a project's `brix_build.ts`, a thin client over builders/status/clean.
 *
 *     process.exit(await cli.main(createGraph, { root, aliases, commands }));
 *
 * `main` is async (deviation #2), because `build` is. Stage-1 core implements
 * build / status / clean / graph / help plus build-then-act `commands` and
 * target `aliases`. The staging-only verbs (`commit`, `discard`, `build
 * --stage`) and the diagnostics `errors`/`env` are deferred with a clear
 * message — they need machinery beyond the core boundary.
 */

import * as path from "path";

import {
  AtomicBuilder,
  BuildOptions,
  GraphSupplier,
  IncrementalBuilder,
  NestableBuilder,
} from "./builders";
import { runClean, runStatus } from "./engine";
import { ExpandAll, ExternalProcessAction } from "./actions";
import { Graph } from "./graph";
import { ConsoleReporter, Reporter } from "./report";
import { BuildResult } from "./results";

/** A target shorthand that also carries a description for `help`. */
export class Alias {
  constructor(
    public readonly target: string | string[],
    public readonly description = "",
  ) {}
}

export type AliasMap = Record<string, string | string[] | Alias>;
export type CommandFn = (paths: string[]) => number | void | Promise<number | void>;
export type CommandMap = Record<string, CommandFn>;

export interface MainOptions {
  root?: string;
  argv?: string[];
  metadataDir?: string;
  stagingDir?: string;
  builderFactory?: () => NestableBuilder | AtomicBuilder;
  aliases?: AliasMap;
  commands?: CommandMap;
}

interface ParsedArgs {
  command: string;
  targets: string[];
  jobs: number | null;
  silent: boolean;
  quietReplays: boolean;
  noDaemon: boolean;
  workerTimeout: number | null;
  json: boolean;
  stage: boolean;
}

const BUILTIN_VERBS = new Set([
  "build",
  "status",
  "clean",
  "graph",
  "help",
  "errors",
  "commit",
  "discard",
  "env",
]);

const DEFERRED = new Set(["errors", "commit", "discard", "env"]);

function parseArgs(argv: string[], commandNames: Set<string>): ParsedArgs {
  const p: ParsedArgs = {
    command: "build",
    targets: [],
    jobs: null,
    silent: false,
    quietReplays: false,
    noDaemon: false,
    workerTimeout: null,
    json: false,
    stage: false,
  };
  let rest = argv;
  if (rest.length && !rest[0].startsWith("-") &&
      (BUILTIN_VERBS.has(rest[0]) || commandNames.has(rest[0]))) {
    p.command = rest[0];
    rest = rest.slice(1);
  }
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    switch (a) {
      case "-j":
      case "--jobs":
        p.jobs = Number(rest[++i]);
        break;
      case "--silent":
        p.silent = true;
        break;
      case "--quiet-replays":
        p.quietReplays = true;
        break;
      case "--no-daemon":
        p.noDaemon = true;
        break;
      case "--worker-timeout":
        p.workerTimeout = Number(rest[++i]);
        break;
      case "--json":
        p.json = true;
        break;
      case "--stage":
        p.stage = true;
        break;
      default:
        if (a.startsWith("-")) throw new Error(`unknown option ${a}`);
        p.targets.push(a);
    }
  }
  return p;
}

function loadGraph(supplier: GraphSupplier): Graph {
  return typeof supplier === "function" ? supplier() : supplier;
}

export async function main(
  graphSupplier: GraphSupplier,
  opts: MainOptions = {},
): Promise<number> {
  const root = path.resolve(opts.root ?? ".");
  const metadataDir = opts.metadataDir ?? "build/.brix";
  const stagingDir = opts.stagingDir ?? "build/staging";
  const aliases = opts.aliases ?? {};
  const commands = opts.commands ?? {};
  const argv = opts.argv ?? process.argv.slice(2);

  let args: ParsedArgs;
  try {
    args = parseArgs(argv, new Set(Object.keys(commands)));
  } catch (e) {
    console.error(`brix: ${(e as Error).message}`);
    return 2;
  }
  const command = args.command;

  const resolveTargets = (): string[] | null => {
    const out: string[] = [];
    for (const t of args.targets) {
      const value = aliases[t] ?? t;
      const target = value instanceof Alias ? value.target : value;
      out.push(...(typeof target === "string" ? [target] : target));
    }
    return out.length ? out : null;
  };

  if (DEFERRED.has(command) || (command === "build" && args.stage)) {
    console.error(
      `brix: '${args.stage ? "build --stage" : command}' is not available in ` +
        "the TypeScript frontend yet (Stage 1 core); use the Python build file " +
        "for staging/diagnostics.",
    );
    return 2;
  }

  if (command === "build" || command in commands) {
    const reporter: Reporter | undefined = args.silent
      ? undefined
      : new ConsoleReporter(!args.quietReplays);
    const builder = opts.builderFactory
      ? opts.builderFactory()
      : new IncrementalBuilder(graphSupplier, { root, metadataDir });
    const buildOpts: BuildOptions & { hold?: boolean } = {
      jobs: args.jobs,
      reporter,
      targets: resolveTargets(),
    };
    if (args.noDaemon) buildOpts.workerIdleTimeout = 0;
    else if (args.workerTimeout !== null) buildOpts.workerIdleTimeout = args.workerTimeout;

    let result: BuildResult;
    try {
      result = await builder.build(buildOpts);
    } catch (e) {
      console.error(`brix: ${(e as Error).message}`);
      return 2;
    }
    if (!result.success) return 1;
    if (command === "build") return 0;
    const paths = (resolveTargets() ?? []).map((t) => path.join(root, t));
    const code = await commands[command](paths);
    return Number(code ?? 0);
  }

  if (command === "status") {
    const report = runStatus(loadGraph(graphSupplier), {
      root,
      metadataDir,
      stagingDir,
      targets: resolveTargets(),
    });
    if (args.json) {
      console.log(JSON.stringify(report, null, 2));
    } else {
      for (const a of report.actions) {
        console.log(`  ${a.state.padEnd(28)} ${a.label}`);
      }
    }
    const clean =
      report.actions.every((a) => a.state === "fresh") &&
      report.pending_deletions.length === 0 &&
      report.staged_files.length === 0;
    return clean ? 0 : 1;
  }

  if (command === "clean") {
    const removed = runClean(loadGraph(graphSupplier), { root, metadataDir });
    console.log(`removed ${removed} file(s)`);
    return 0;
  }

  if (command === "graph") {
    console.log(describeGraph(loadGraph(graphSupplier)));
    return 0;
  }

  if (command === "help") {
    console.log(renderHelp(opts, aliases, commands));
    return 0;
  }

  console.error(`brix: unknown command '${command}'`);
  return 2;
}

/** A structural dump of the logical graph (not the realized plan). */
function describeGraph(graph: Graph): string {
  const lines: string[] = [];
  const walk = (action: unknown, indent: string): void => {
    if (action instanceof ExpandAll) {
      lines.push(`${indent}expand {${action.elementName}}`);
      walk(action.child, indent + "  ");
    } else if (action instanceof ExternalProcessAction) {
      lines.push(`${indent}${action.name ?? action.commandLine}`);
    }
  };
  for (const a of graph.actions) walk(a, "");
  return lines.join("\n") || "(empty graph)";
}

function renderHelp(
  opts: MainOptions,
  aliases: AliasMap,
  commands: CommandMap,
): string {
  const lines: string[] = ["Commands:"];
  const rows: [string, string][] = [
    ["build [targets]", "build the requested targets, or everything (default)"],
    ["status [targets] [--json]", "what would build, and why; exit 1 if pending"],
    ["clean", "delete built outputs and metadata"],
    ["graph", "print the logical graph structure"],
    ["help", "show this configuration"],
  ];
  for (const name of Object.keys(commands)) {
    rows.push([`${name} <targets>`, `build target(s), then ${name} them`]);
  }
  const width = Math.max(...rows.map(([n]) => n.length));
  for (const [name, summary] of rows) lines.push(`  ${name.padEnd(width)}  ${summary}`);

  const aliasNames = Object.keys(aliases);
  if (aliasNames.length) {
    lines.push("", "Targets (aliases):");
    const w = Math.max(...aliasNames.map((n) => n.length));
    for (const name of aliasNames) {
      const value = aliases[name];
      const target = value instanceof Alias ? value.target : value;
      const desc = value instanceof Alias && value.description ? `   ${value.description}` : "";
      const shown = typeof target === "string" ? target : target.join(" ");
      lines.push(`  ${name.padEnd(w)}  → ${shown}${desc}`);
    }
  }
  return lines.join("\n");
}
