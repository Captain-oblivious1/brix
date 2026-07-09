//! The scheduler: runs an execution plan. Mirrors `src/brix/executor.py`
//! structure-for-structure — the do-not-reinterpret pieces (docs/rust-port.md)
//! are called out inline:
//!
//! - readiness: ON_SUCCESS needs DONE; ON_COMPLETION needs DONE *or* FAILED
//!   (a SKIPPED predecessor satisfies neither — that is what makes an
//!   on-completion successor of a skipped node a quiet stuck-skip);
//! - the failure cascade walks only on-success edges;
//! - stuck nodes are judged against one pre-advance **snapshot**: all-terminal
//!   predecessors -> quiet skip, otherwise "unreachable (dependency cycle?)";
//! - the insertion channel is the only way plan structure grows mid-build
//!   (runtime expansion and layer proposals), and proposals are ordered
//!   before the source expansion's consumers.
//!
//! Threading: plain scoped threads and channels (no async runtime — process
//! spawning is blocking per worker, matching the Python thread pool).

use std::collections::{BTreeSet, HashMap};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

use crate::actions::Action;
use crate::context::NodeContext;
use crate::data::DataAtom;
use crate::layers::{BuildLayer, NodeProposal};
use crate::plan::{EdgeTrigger, ExecutionPlan, Node, NodeKind, NodeState, NodeStateTable};
use crate::process::{run_process, ProcessResult};
use crate::render::{prepare_output_dirs, render_command, render_env};
use crate::workers::WorkerRegistry;
use crate::report::Reporter;

/// The outcome of one `execute()`. (Python's `BuildResult` also carries the
/// plan; here the caller keeps its `&mut ExecutionPlan`.)
#[derive(Debug)]
pub struct BuildResult {
    pub success: bool,
    pub states: NodeStateTable,
    pub process_results: Vec<ProcessResult>,
    pub errors: Vec<String>,
}

fn panic_message(panic: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = panic.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = panic.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    }
}

fn run_delete(action: &Action, ctx: &NodeContext) -> ProcessResult {
    let Action::Delete { paths } = action else { unreachable!() };
    let label = action.label();
    let mut deleted: Vec<String> = Vec::new();
    for rel in paths {
        let target = ctx.output_root.join(rel);
        if target.exists() {
            if let Err(exc) = std::fs::remove_file(&target) {
                return ProcessResult {
                    action_name: label.clone(),
                    command: label.clone(),
                    exit_code: Some(1),
                    stderr: format!("brix: cannot delete: {exc}"),
                    source: "process".to_string(),
                    ..Default::default()
                };
            }
            deleted.push(rel.clone());
        }
    }
    ProcessResult {
        action_name: label.clone(),
        command: label,
        exit_code: Some(0),
        stdout: deleted.iter().map(|p| format!("deleted {p}\n")).collect(),
        source: "process".to_string(),
        ..Default::default()
    }
}

fn run_process_node(node: &Node, ctx: &NodeContext, workers: &WorkerRegistry) -> ProcessResult {
    let action = &node.action;
    if let Action::Delete { .. } = action {
        return run_delete(action, ctx);
    }
    let label = action.label();
    if let Action::Process { worker: Some(spec), .. } = action {
        // Same rendering as a spawn (overlay output-redirection included),
        // but the argv is dispatched to a worker instead of spawned. Env is
        // not part of the worker protocol; worker actions do not use env= in
        // v1. (Mirrors `_run_process_node`.)
        let args = match (|| {
            prepare_output_dirs(action, ctx)?;
            crate::shlex::split(&render_command(action, ctx)?)
        })() {
            Ok(args) => args,
            Err(exc) => return ProcessResult::did_not_run(&label, "", format!("brix: {exc}")),
        };
        return workers.run(spec, &label, args, ctx);
    }
    let (command, env) = match (|| {
        prepare_output_dirs(action, ctx)?;
        let command = render_command(action, ctx)?;
        let env = render_env(action, ctx)?;
        Ok::<_, String>((command, env))
    })() {
        Ok(pair) => pair,
        Err(exc) => return ProcessResult::did_not_run(&label, "", format!("brix: {exc}")),
    };
    run_process(&label, &command, &ctx.workspace_root, &env)
}

/// Run the node through the layer stack, innermost first in the given list
/// (so the last layer is outermost — mirrors `_stacked_runner`). The base of
/// the stack fires `process_started` — only reached when a real process is
/// about to spawn (a replaying layer returns before this), so the reporter's
/// liveness signal is exact. Runs on a worker thread.
fn run_stacked(
    layers: &[&dyn BuildLayer],
    depth: usize,
    node: &Node,
    ctx: &NodeContext,
    reporter: &dyn Reporter,
    workers: &WorkerRegistry,
) -> ProcessResult {
    if depth == 0 {
        reporter.process_started(node);
        return run_process_node(node, ctx, workers);
    }
    layers[depth - 1].around_node(node, ctx, &|inner_ctx: &NodeContext| {
        run_stacked(layers, depth - 1, node, inner_ctx, reporter, workers)
    })
}

/// A Python-`repr`-shaped rendering of a collection atom for error messages
/// (not pinned by any contract test; kept close to `data.py.__repr__`).
fn atom_repr(atom: &DataAtom) -> String {
    match atom {
        DataAtom::Glob { template, .. } => format!("data.glob('{template}')"),
        DataAtom::Regex { pattern } => format!("data.regex('{pattern}')"),
        DataAtom::File { path, .. } => format!("data.file('{path}')"),
        DataAtom::Tool { name } => format!("data.tool('{name}')"),
        DataAtom::Value { text } => format!("data.value('{text}')"),
    }
}

/// Realize the collection (through the overlay) and return sorted unique
/// element bindings (mirrors `_collection_members`).
fn collection_members(action: &Action, ctx: &NodeContext) -> Result<Vec<String>, String> {
    let Action::Expand { element, collection, .. } = action else { unreachable!() };
    let mut members: Vec<String> = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    for atom in collection {
        let found: Vec<Vec<(String, String)>> = match atom {
            DataAtom::Glob { .. } => {
                let pattern = atom.path_pattern().ok_or("bad glob pattern")?;
                ctx.find(&pattern).into_iter().map(|(_, _, b)| b).collect()
            }
            DataAtom::Regex { .. } => ctx
                .iter_files()
                .into_iter()
                .filter_map(|(logical, _)| atom.regex_match(&logical))
                .collect(),
            _ => continue,
        };
        for bindings in found {
            let value = bindings
                .iter()
                .find(|(k, _)| k == element)
                .map(|(_, v)| v.clone())
                .ok_or_else(|| {
                    format!("collection {} does not bind '{element}'", atom_repr(atom))
                })?;
            if seen.insert(value.clone()) {
                members.push(value);
            }
        }
    }
    members.sort();
    Ok(members)
}

/// Expand an ExpandAll node into concrete sibling nodes (mirrors `_expand`).
/// Returns the member values and the new nodes' ids.
fn expand(
    plan: &mut ExecutionPlan,
    nid: usize,
    ctx: &NodeContext,
) -> Result<(Vec<String>, Vec<usize>), String> {
    let action = plan.nodes[nid].action.clone();
    let Action::Expand { element, .. } = &action else { unreachable!() };
    let members = collection_members(&action, ctx)?;
    let pred_ids = plan.predecessor_ids(nid);
    let succ_ids = plan.successor_ids(nid);
    let mut children: Vec<usize> = Vec::new();
    for value in &members {
        let Action::Expand { child, .. } = &action else { unreachable!() };
        let concrete = child.substitute(&[(element.clone(), value.clone())]);
        // A nested Expand becomes another expansion point, which the
        // scheduler will expand in turn once it is ready.
        let kind = if matches!(concrete, Action::Expand { .. }) {
            NodeKind::Expand
        } else {
            NodeKind::Process
        };
        let child_id = plan.add_node(kind, concrete.label(), concrete);
        for &pid in &pred_ids {
            plan.add_edge(pid, child_id, format!("expansion of [{nid}]"));
        }
        for &sid in &succ_ids {
            // Python: f"expanded member {value!r}" (repr quotes).
            plan.add_edge(child_id, sid, format!("expanded member '{value}'"));
        }
        children.push(child_id);
    }
    Ok((members, children))
}

pub fn execute(
    plan: &mut ExecutionPlan,
    jobs: Option<usize>,
    layers: &[&dyn BuildLayer],
    reporter: &dyn Reporter,
) -> BuildResult {
    let mut states = NodeStateTable::new();
    let mut ctx = NodeContext::for_root(std::path::PathBuf::from(&plan.root));
    // Python: `jobs or min(32, cpu_count or 4)` — 0 is falsy and falls back.
    let max_workers = jobs.filter(|&j| j > 0).unwrap_or_else(|| {
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(32)
    });
    let mut pending: BTreeSet<usize> = plan.nodes.iter().map(|n| n.id).collect();
    let mut running: usize = 0;
    let mut results: Vec<ProcessResult> = Vec::new();
    let mut errors: Vec<String> = Vec::new();

    reporter.build_begin(plan);
    for layer in layers {
        layer.on_build_begin(plan, &mut ctx);
    }
    // From here the context is frozen and shared with worker threads.
    let ctx = ctx;
    let workers = WorkerRegistry::new();

    let (job_tx, job_rx) = mpsc::channel::<(usize, Node)>();
    let (done_tx, done_rx) = mpsc::channel::<(usize, ProcessResult)>();
    let job_rx = Arc::new(Mutex::new(job_rx));

    std::thread::scope(|s| {
        for _ in 0..max_workers {
            let job_rx = Arc::clone(&job_rx);
            let done_tx = done_tx.clone();
            let ctx = &ctx;
            let workers = &workers;
            s.spawn(move || loop {
                // Hold the lock only for the recv; execution is parallel.
                let msg = job_rx.lock().unwrap().recv();
                match msg {
                    Ok((nid, node)) => {
                        // A panic in a layer or runner must not strand the
                        // scheduler waiting on this job (Python re-raises via
                        // future.result(); the build dies either way — here it
                        // dies as a failed node instead of a hang).
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                            || run_stacked(layers, layers.len(), &node, ctx, reporter, workers),
                        ))
                        .unwrap_or_else(|panic| {
                            let msg = panic_message(&panic);
                            ProcessResult::did_not_run(
                                &node.label,
                                "",
                                format!("brix: internal error (worker panicked): {msg}"),
                            )
                        });
                        if done_tx.send((nid, result)).is_err() {
                            break;
                        }
                    }
                    Err(_) => break, // job channel closed: build is over
                }
            });
        }
        drop(done_tx); // workers hold the only senders now

        // -- helpers over the scheduler's own state --------------------------

        fn ready(plan: &ExecutionPlan, states: &NodeStateTable, node_id: usize) -> bool {
            for edge in plan.incoming(node_id) {
                let status = states.status(edge.from_id);
                match edge.trigger {
                    EdgeTrigger::OnCompletion => {
                        if !matches!(status, NodeState::Done | NodeState::Failed) {
                            return false;
                        }
                    }
                    EdgeTrigger::OnSuccess => {
                        if status != NodeState::Done {
                            return false;
                        }
                    }
                }
            }
            true
        }

        fn success_successor_ids(plan: &ExecutionPlan, node_id: usize) -> Vec<usize> {
            plan.edges
                .iter()
                .filter(|e| e.from_id == node_id && e.trigger == EdgeTrigger::OnSuccess)
                .map(|e| e.to_id)
                .collect()
        }

        // Only on-success edges cascade; on-completion successors still run.
        fn cascade_skip(
            plan: &ExecutionPlan,
            states: &mut NodeStateTable,
            pending: &mut BTreeSet<usize>,
            reporter: &dyn Reporter,
            from_id: usize,
            why: &str,
        ) {
            let mut stack = success_successor_ids(plan, from_id);
            while let Some(nid) = stack.pop() {
                if states.status(nid) != NodeState::Pending {
                    continue;
                }
                states.advance(nid, NodeState::Skipped, None);
                pending.remove(&nid);
                reporter.node_skipped(&plan.nodes[nid], why);
                stack.extend(success_successor_ids(plan, nid));
            }
        }

        // -- the scheduler loop ----------------------------------------------

        while !pending.is_empty() || running > 0 {
            let mut scheduled = false;
            let snapshot: Vec<usize> = pending.iter().copied().collect(); // sorted
            for nid in snapshot {
                // A cascade earlier in this pass may have skipped it already.
                if !pending.contains(&nid) {
                    continue;
                }
                if !ready(plan, &states, nid) {
                    continue;
                }
                pending.remove(&nid);
                scheduled = true;
                if plan.nodes[nid].kind == NodeKind::Expand {
                    match expand(plan, nid, &ctx) {
                        Ok((members, children)) => {
                            states.advance(nid, NodeState::Done, None);
                            pending.extend(children.iter().copied());
                            let child_refs: Vec<&Node> =
                                children.iter().map(|&c| &plan.nodes[c]).collect();
                            reporter.collection_realized(
                                &plan.nodes[nid],
                                &members,
                                &child_refs,
                            );
                            let mut proposals: Vec<NodeProposal> = Vec::new();
                            for layer in layers {
                                proposals.extend(layer.on_collection_realized(
                                    &plan.nodes[nid],
                                    &members,
                                    &ctx,
                                ));
                            }
                            // The insertion channel: the scheduler inserts and
                            // wires proposed nodes, ordered before the source
                            // expansion's consumers.
                            let consumer_ids = plan.successor_ids(nid);
                            for proposal in proposals {
                                let label = proposal
                                    .label
                                    .unwrap_or_else(|| proposal.action.label());
                                let new_id =
                                    plan.add_node(NodeKind::Process, label, proposal.action);
                                let reason = format!("cleanup proposed at [{nid}]");
                                for &cid in &consumer_ids {
                                    plan.add_edge(new_id, cid, reason.clone());
                                }
                                pending.insert(new_id);
                                reporter.node_inserted(&plan.nodes[new_id], &reason);
                            }
                        }
                        Err(exc) => {
                            states.advance(nid, NodeState::Failed, None);
                            errors.push(format!("{}: {exc}", plan.nodes[nid].label));
                            reporter.node_completed(
                                &plan.nodes[nid],
                                NodeState::Failed,
                                None,
                                Some(&exc),
                            );
                            cascade_skip(
                                plan,
                                &mut states,
                                &mut pending,
                                reporter,
                                nid,
                                &format!("upstream failed: {}", plan.nodes[nid].label),
                            );
                        }
                    }
                } else {
                    states.advance(nid, NodeState::Running, None);
                    reporter.node_started(&plan.nodes[nid]);
                    // The worker gets its own copy: the plan may grow (and
                    // reallocate) while the job runs.
                    job_tx.send((nid, plan.nodes[nid].clone())).expect("workers alive");
                    running += 1;
                }
            }

            if running > 0 {
                // Python: wait(FIRST_COMPLETED) then handle every done future.
                let mut finished: Vec<(usize, ProcessResult)> =
                    vec![done_rx.recv().expect("a worker panicked")];
                while let Ok(more) = done_rx.try_recv() {
                    finished.push(more);
                }
                for (nid, result) in finished {
                    running -= 1;
                    let ok = result.ok();
                    let summary = result.summary();
                    results.push(result.clone());
                    if ok {
                        states.advance(nid, NodeState::Done, Some(result.clone()));
                        reporter.node_completed(
                            &plan.nodes[nid],
                            NodeState::Done,
                            Some(&result),
                            None,
                        );
                    } else {
                        states.advance(nid, NodeState::Failed, Some(result.clone()));
                        errors.push(summary);
                        reporter.node_completed(
                            &plan.nodes[nid],
                            NodeState::Failed,
                            Some(&result),
                            None,
                        );
                        cascade_skip(
                            plan,
                            &mut states,
                            &mut pending,
                            reporter,
                            nid,
                            &format!("upstream failed: {}", plan.nodes[nid].label),
                        );
                    }
                }
            } else if !scheduled && !pending.is_empty() {
                // Judge every stuck node against the same snapshot: advancing
                // one to SKIPPED must not soften the verdict on the next.
                let verdicts: HashMap<usize, bool> = pending
                    .iter()
                    .map(|&nid| {
                        (
                            nid,
                            plan.incoming(nid)
                                .all(|e| states.status(e.from_id).is_terminal()),
                        )
                    })
                    .collect();
                let stuck: Vec<usize> = pending.iter().copied().collect(); // sorted
                for nid in stuck {
                    states.advance(nid, NodeState::Skipped, None);
                    if verdicts[&nid] {
                        // e.g. an on-completion successor of a skipped node
                        reporter.node_skipped(&plan.nodes[nid], "upstream skipped");
                    } else {
                        errors.push(format!(
                            "{}: unreachable (dependency cycle?)",
                            plan.nodes[nid].label
                        ));
                        reporter
                            .node_skipped(&plan.nodes[nid], "unreachable — dependency cycle?");
                    }
                }
                pending.clear();
            }
        }

        drop(job_tx); // closes the job channel: workers exit, scope joins them
    });

    // Scheduler owns worker lifecycles: all terminated at build end (v1).
    workers.terminate_all();
    let success = errors.is_empty();
    let build_result = BuildResult { success, states, process_results: results, errors };
    for layer in layers {
        layer.on_build_end(&build_result, &ctx);
    }
    reporter.build_end(&build_result);
    build_result
}
