//! The scheduler contract, mirrored from `tests/test_scheduler_contract.py`
//! — either engine must satisfy these. Same cases, same names: the
//! event-ordering contract, the stuck-node **snapshot** rule, the
//! on-success-only failure cascade, and insertion-channel ordering. Plus an
//! end-to-end clean build through `spec::parse` -> `plan_graph` -> `execute`
//! (the wire path the Python binding will drive).
//!
//! Commands use `true`/`false`/`sh -c` rather than a Python interpreter so
//! the suite is self-contained on any POSIX dev machine.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use brix_core::actions::Action;
use brix_core::executor::{execute, BuildResult};
use brix_core::layers::{BuildLayer, NodeProposal};
use brix_core::plan::{EdgeTrigger, ExecutionPlan, Node, NodeKind, NodeState};
use brix_core::process::ProcessResult;
use brix_core::report::Reporter;

// -- helpers ------------------------------------------------------------------

fn ok_action(name: &str) -> Action {
    Action::Process {
        name: Some(name.to_string()),
        command_line: "true".to_string(),
        inputs: vec![],
        outputs: vec![],
        env: vec![],
    }
}

fn fail_action() -> Action {
    Action::Process {
        name: Some("boom".to_string()),
        command_line: "false".to_string(),
        inputs: vec![],
        outputs: vec![],
        env: vec![],
    }
}

/// A fresh scratch workspace per test (no tempfile dependency).
fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-executor-{name}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write(root: &PathBuf, rel: &str, content: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
}

/// Captures the full event stream so ordering can be asserted (the Python
/// `Recorder`).
#[derive(Default)]
struct Recorder {
    events: Mutex<Vec<(String, String)>>,
}

impl Recorder {
    fn push(&self, kind: &str, label: &str) {
        self.events.lock().unwrap().push((kind.to_string(), label.to_string()));
    }

    fn events(&self) -> Vec<(String, String)> {
        self.events.lock().unwrap().clone()
    }

    fn index(&self, kind: &str, label: Option<&str>) -> usize {
        let events = self.events();
        events
            .iter()
            .position(|(k, l)| k == kind && label.map_or(true, |want| l == want))
            .unwrap_or_else(|| panic!("no {kind} {label:?} in {events:?}"))
    }
}

impl Reporter for Recorder {
    fn build_begin(&self, _plan: &ExecutionPlan) {
        self.push("begin", "");
    }
    fn node_started(&self, node: &Node) {
        self.push("started", &node.label);
    }
    fn process_started(&self, node: &Node) {
        self.push("process", &node.label);
    }
    fn node_completed(
        &self,
        node: &Node,
        _status: NodeState,
        _result: Option<&ProcessResult>,
        _error: Option<&str>,
    ) {
        self.push("completed", &node.label);
    }
    fn node_skipped(&self, node: &Node, _why: &str) {
        self.push("skipped", &node.label);
    }
    fn collection_realized(&self, node: &Node, _members: &[String], _children: &[&Node]) {
        self.push("realized", &node.label);
    }
    fn node_inserted(&self, node: &Node, _reason: &str) {
        self.push("inserted", &node.label);
    }
    fn build_end(&self, _result: &BuildResult) {
        self.push("end", "");
    }
}

/// The upper+combine graph from the Python contract file, wire-shaped.
/// `tr`/`cat` under `sh -c` replace the Python helper scripts.
fn expand_then_combine_spec() -> String {
    r#"{
      "version": 1,
      "actions": [
        {
          "kind": "expand",
          "element": "name",
          "collection": [{"kind": "glob", "template": "src/{name}.txt"}],
          "child": {
            "kind": "process",
            "name": "upper {name}",
            "command_line": "sh -c 'tr a-z A-Z < {source} > {dest}'",
            "inputs": {"source": [{"kind": "file", "path": "src/{name}.txt"}]},
            "outputs": {"dest": [{"kind": "file", "path": "build/upper/{name}.txt"}]}
          }
        },
        {
          "kind": "process",
          "name": "combine",
          "command_line": "sh -c 'cat {parts} > {combined}'",
          "inputs": {"parts": [{"kind": "glob", "template": "build/upper/{name}.txt"}]},
          "outputs": {"combined": [{"kind": "file", "path": "build/all.txt"}]}
        }
      ]
    }"#
    .to_string()
}

fn plan_from_spec(spec_json: &str, root: &PathBuf) -> ExecutionPlan {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap()
}

// -- end-to-end clean build ---------------------------------------------------

#[test]
fn clean_build_expands_and_combines() {
    let root = scratch("clean");
    write(&root, "src/a.txt", "a");
    write(&root, "src/b.txt", "b");
    let mut plan = plan_from_spec(&expand_then_combine_spec(), &root);
    let result = execute(&mut plan, Some(2), &[], &brix_core::report::NullReporter);
    assert!(result.success, "errors: {:?}", result.errors);
    assert_eq!(std::fs::read_to_string(root.join("build/upper/a.txt")).unwrap(), "A");
    assert_eq!(std::fs::read_to_string(root.join("build/all.txt")).unwrap(), "AB");
}

// -- event ordering -----------------------------------------------------------

#[test]
fn build_begin_is_first_and_build_end_is_last() {
    let root = scratch("events-ends");
    write(&root, "src/a.txt", "a");
    let mut plan = plan_from_spec(&expand_then_combine_spec(), &root);
    let r = Recorder::default();
    assert!(execute(&mut plan, Some(1), &[], &r).success);
    let events = r.events();
    assert_eq!(events.first().unwrap().0, "begin");
    assert_eq!(events.last().unwrap().0, "end");
}

#[test]
fn per_node_started_then_process_then_completed() {
    let root = scratch("events-order");
    write(&root, "src/a.txt", "a");
    let mut plan = plan_from_spec(&expand_then_combine_spec(), &root);
    let r = Recorder::default();
    assert!(execute(&mut plan, Some(1), &[], &r).success);
    // For one node the order is fixed: node_started (scheduler) <
    // process_started (worker, before the spawn) < node_completed (scheduler).
    assert!(r.index("started", Some("upper a")) < r.index("process", Some("upper a")));
    assert!(r.index("process", Some("upper a")) < r.index("completed", Some("upper a")));
}

#[test]
fn collection_realized_precedes_its_children() {
    let root = scratch("events-realized");
    write(&root, "src/a.txt", "a");
    let mut plan = plan_from_spec(&expand_then_combine_spec(), &root);
    let r = Recorder::default();
    assert!(execute(&mut plan, Some(1), &[], &r).success);
    assert!(r.index("realized", None) < r.index("started", Some("upper a")));
}

// -- stuck-node snapshot rule -------------------------------------------------

#[test]
fn two_node_cycle_reports_both_nodes_not_one() {
    // The snapshot rule: every stuck node is judged against the same
    // pre-advance state, so BOTH cycle nodes are "unreachable". A naive
    // advance-then-rejudge would flip the first to SKIPPED and then
    // quiet-skip the second.
    let root = scratch("cycle");
    let mut plan = ExecutionPlan::new(root.to_str().unwrap());
    let a = plan.add_node(NodeKind::Process, "a".into(), ok_action("a"));
    let b = plan.add_node(NodeKind::Process, "b".into(), ok_action("b"));
    plan.add_edge(a, b, "a->b".into());
    plan.add_edge(b, a, "b->a".into());

    let result = execute(&mut plan, Some(1), &[], &brix_core::report::NullReporter);
    assert!(!result.success);
    let unreachable: Vec<_> =
        result.errors.iter().filter(|e| e.contains("unreachable")).collect();
    assert_eq!(unreachable.len(), 2); // both, not one
    assert_eq!(result.states.status(a), NodeState::Skipped);
    assert_eq!(result.states.status(b), NodeState::Skipped);
}

#[test]
fn stuck_node_with_terminal_predecessor_is_a_quiet_skip() {
    // `boom` fails, cascade-skips `a` (on-success); `b` waits on `a` via
    // on-completion, which never fires (a is SKIPPED), so `b` goes stuck
    // with a terminal predecessor -> quiet skip, not an "unreachable" error.
    let root = scratch("quiet-skip");
    let mut plan = ExecutionPlan::new(root.to_str().unwrap());
    let boom = plan.add_node(NodeKind::Process, "boom".into(), fail_action());
    let a = plan.add_node(NodeKind::Process, "a".into(), ok_action("a"));
    let b = plan.add_node(NodeKind::Process, "b".into(), ok_action("b"));
    plan.add_edge(boom, a, "boom->a".into());
    plan.add_edge_triggered(a, b, "a->b".into(), EdgeTrigger::OnCompletion);

    let result = execute(&mut plan, Some(1), &[], &brix_core::report::NullReporter);
    assert!(!result.success); // boom failed
    assert_eq!(result.states.status(a), NodeState::Skipped); // cascade
    assert_eq!(result.states.status(b), NodeState::Skipped); // stuck -> quiet
    assert!(!result.errors.iter().any(|e| e.contains("unreachable")));
}

// -- on-success-only failure cascade ------------------------------------------

#[test]
fn failure_cascades_transitively_along_on_success_edges() {
    // A failure skips the whole downstream on-success chain, and those
    // cleanly-cascaded nodes are NOT reported as "unreachable".
    let root = scratch("cascade");
    let mut plan = ExecutionPlan::new(root.to_str().unwrap());
    let boom = plan.add_node(NodeKind::Process, "boom".into(), fail_action());
    let a = plan.add_node(NodeKind::Process, "a".into(), ok_action("a"));
    let b = plan.add_node(NodeKind::Process, "b".into(), ok_action("b"));
    let c = plan.add_node(NodeKind::Process, "c".into(), ok_action("c"));
    plan.add_edge(boom, a, "boom->a".into());
    plan.add_edge(a, b, "a->b".into());
    plan.add_edge(b, c, "b->c".into());

    let result = execute(&mut plan, Some(1), &[], &brix_core::report::NullReporter);
    assert!(!result.success);
    for node in [a, b, c] {
        assert_eq!(result.states.status(node), NodeState::Skipped);
    }
    assert!(!result.errors.iter().any(|e| e.contains("unreachable")));
}

#[test]
fn on_completion_reactivates_a_branch_past_a_failure() {
    // The cascade walks only on-success edges: an on-completion successor of
    // a failed node runs, and its own on-success successor then runs too.
    let root = scratch("reactivate");
    let mut plan = ExecutionPlan::new(root.to_str().unwrap());
    let boom = plan.add_node(NodeKind::Process, "boom".into(), fail_action());
    let a = plan.add_node(NodeKind::Process, "a".into(), ok_action("a"));
    let b = plan.add_node(NodeKind::Process, "b".into(), ok_action("b"));
    plan.add_edge_triggered(boom, a, "boom->a".into(), EdgeTrigger::OnCompletion);
    plan.add_edge(a, b, "a->b".into());

    let result = execute(&mut plan, Some(1), &[], &brix_core::report::NullReporter);
    assert!(!result.success); // boom still failed the build
    assert_eq!(result.states.status(a), NodeState::Done); // ran despite the failure
    assert_eq!(result.states.status(b), NodeState::Done); // branch reactivated
}

// -- insertion-channel ordering -----------------------------------------------

/// A layer that proposes one stale-output deletion when the collection is
/// realized (standing in for the incremental layer's cleanup proposals).
struct ProposeDelete {
    path: String,
}

impl BuildLayer for ProposeDelete {
    fn on_collection_realized(
        &self,
        _node: &Node,
        _members: &[String],
        _ctx: &brix_core::context::NodeContext,
    ) -> Vec<NodeProposal> {
        vec![NodeProposal {
            action: Action::Delete { paths: vec![self.path.clone()] },
            label: None,
        }]
    }
}

#[test]
fn proposed_cleanup_runs_before_the_expansions_consumer() {
    // Layer proposals from on_collection_realized are inserted ordered
    // *before* the expansion's consumers: the stale-output deletion completes
    // before `combine` reads the collection.
    let root = scratch("insertion");
    write(&root, "src/a.txt", "a");
    write(&root, "build/upper/stale.txt", "STALE"); // debris a layer wants gone
    let mut plan = plan_from_spec(&expand_then_combine_spec(), &root);
    let layer = ProposeDelete { path: "build/upper/stale.txt".to_string() };
    let r = Recorder::default();
    let result = execute(&mut plan, Some(1), &[&layer], &r);
    assert!(result.success, "errors: {:?}", result.errors);

    // A cleanup node was inserted, and it completed before combine started.
    let events = r.events();
    assert!(events.iter().any(|(k, _)| k == "inserted"));
    let delete_completed = events
        .iter()
        .position(|(k, l)| k == "completed" && l.starts_with("rm "))
        .expect("no rm completion");
    assert!(delete_completed < r.index("started", Some("combine")));
    assert!(!root.join("build/upper/stale.txt").exists());
    // ... so combine saw only the fresh member.
    assert_eq!(std::fs::read_to_string(root.join("build/all.txt")).unwrap(), "A");
}

// -- nested expansion -----------------------------------------------------------

#[test]
fn nested_expansion_builds_per_video_per_scene() {
    // An Expand child substituted per member becomes another EXPAND node,
    // which the scheduler expands in turn (mirrors
    // test_nested_expand.py::test_nested_expansion_builds_per_video_per_scene).
    let root = scratch("nested");
    write(&root, "videos/alpha.py", "# alpha\n");
    write(&root, "videos/beta.py", "# beta\n");
    write(&root, "videos/alpha/01.py", "alpha 01");
    write(&root, "videos/alpha/02.py", "alpha 02");
    write(&root, "videos/beta/01.py", "beta 01");
    let spec = r#"{
      "version": 1,
      "actions": [
        {
          "kind": "expand",
          "element": "video",
          "collection": [{"kind": "glob", "template": "videos/{video:*}.py"}],
          "child": {
            "kind": "expand",
            "element": "scene",
            "collection": [{"kind": "glob", "template": "videos/{video:*}/{scene:*}.py"}],
            "child": {
              "kind": "process",
              "name": "render {video}/{scene}",
              "command_line": "sh -c 'tr a-z A-Z < {source} > {out}'",
              "inputs": {"source": [{"kind": "file", "path": "videos/{video}/{scene}.py"}]},
              "outputs": {"out": [{"kind": "file", "path": "build/{video}/{scene}.out"}]}
            }
          }
        }
      ]
    }"#;
    let mut plan = plan_from_spec(spec, &root);
    let result = execute(&mut plan, Some(2), &[], &brix_core::report::NullReporter);
    assert!(result.success, "errors: {:?}", result.errors);
    assert_eq!(std::fs::read_to_string(root.join("build/alpha/01.out")).unwrap(), "ALPHA 01");
    assert_eq!(std::fs::read_to_string(root.join("build/alpha/02.out")).unwrap(), "ALPHA 02");
    assert_eq!(std::fs::read_to_string(root.join("build/beta/01.out")).unwrap(), "BETA 01");
    // Outer expand -> 2 inner expand nodes -> 3 render nodes.
    let expands = plan.nodes.iter().filter(|n| n.kind == NodeKind::Expand).count();
    let processes = plan.nodes.iter().filter(|n| n.kind == NodeKind::Process).count();
    assert_eq!(expands, 3); // outer + one per video
    assert_eq!(processes, 3);
    assert!(plan.nodes.iter().any(|n| n.label == "expand: render alpha/{scene}"));
    assert!(plan.nodes.iter().any(|n| n.label == "render alpha/01"));
}

// -- failure results and did-not-run diagnostics --------------------------------

#[test]
fn render_error_is_a_failed_node_not_a_crash() {
    // An unknown placeholder is a did-not-run result: exit_code None,
    // diagnostic on stderr, build fails.
    let root = scratch("render-error");
    let mut plan = ExecutionPlan::new(root.to_str().unwrap());
    let bad = plan.add_node(
        NodeKind::Process,
        "bad".into(),
        Action::Process {
            name: Some("bad".to_string()),
            command_line: "echo {nope}".to_string(),
            inputs: vec![],
            outputs: vec![],
            env: vec![],
        },
    );
    let result = execute(&mut plan, Some(1), &[], &brix_core::report::NullReporter);
    assert!(!result.success);
    assert_eq!(result.states.status(bad), NodeState::Failed);
    let r = &result.process_results[0];
    assert_eq!(r.exit_code, None);
    assert!(r.stderr.contains("unknown placeholder {nope}"), "stderr: {}", r.stderr);
    assert_eq!(result.errors, vec!["FAILED (did not run): bad".to_string()]);
}
