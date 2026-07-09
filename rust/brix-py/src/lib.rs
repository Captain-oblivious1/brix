//! The PyO3 boundary: `brix_py.build(spec_json, config_json, on_event)`.
//!
//! One function, three JSON strings — the graph spec and build config go in
//! (shapes pinned by `docs/boundary-schema.md`), the build result comes back,
//! and each event crosses as one JSON string through the optional `on_event`
//! callable. Strings, not rich objects, keep this surface language-neutral:
//! the future JS/TS binding is this same file with a different FFI prologue.
//!
//! Threading: the GIL is released for the whole build (processes spawn on
//! worker threads) and re-acquired per event. `process_started` events
//! originate on worker threads — the callback must tolerate that, exactly as
//! `docs/boundary-schema.md` specifies. A callback exception does not tear
//! down the build mid-flight; it is re-raised once the build finishes.

use std::sync::Mutex;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use brix_core::atomic::AtomicLayer;
use brix_core::cache::{CacheLayer, CacheStore};
use brix_core::executor::{execute, BuildResult};
use brix_core::incremental::IncrementalLayer;
use brix_core::layers::BuildLayer;
use brix_core::metadata::BuildMetadata;
use brix_core::plan::{ExecutionPlan, Node, NodeKind, NodeState};
use brix_core::process::ProcessResult;
use brix_core::report::{NullReporter, Reporter};

fn kind_str(kind: NodeKind) -> &'static str {
    match kind {
        NodeKind::Process => "process",
        NodeKind::Expand => "expand",
    }
}

fn state_str(state: NodeState) -> &'static str {
    match state {
        NodeState::Pending => "pending",
        NodeState::Running => "running",
        NodeState::Done => "done",
        NodeState::Failed => "failed",
        NodeState::Skipped => "skipped",
    }
}

fn node_json(node: &Node) -> Value {
    json!({"id": node.id, "label": node.label, "kind": kind_str(node.kind)})
}

fn result_json(result: &BuildResult, plan: &ExecutionPlan) -> Value {
    let mut states = Map::new();
    let mut node_results = Map::new();
    for node in &plan.nodes {
        states.insert(node.id.to_string(), json!(state_str(result.states.status(node.id))));
        if let Some(r) = result.states.result(node.id) {
            node_results.insert(
                node.id.to_string(),
                serde_json::to_value(r).expect("result serializes"),
            );
        }
    }
    json!({
        "success": result.success,
        "errors": result.errors,
        "states": states,
        "node_results": node_results,
        "process_results": serde_json::to_value(&result.process_results)
            .expect("results serialize"),
        "plan": {"nodes": plan.nodes.iter().map(node_json).collect::<Vec<_>>()},
    })
}

/// Bridges core events to the Python callback as JSON strings. The first
/// callback exception is kept and re-raised after the build; later events
/// are still delivered (the build cannot be stopped mid-flight anyway).
struct PyReporter {
    callback: Py<PyAny>,
    error: Mutex<Option<PyErr>>,
}

impl PyReporter {
    fn emit(&self, event: Value) {
        Python::attach(|py| {
            if let Err(err) = self.callback.call1(py, (event.to_string(),)) {
                let mut slot = self.error.lock().unwrap();
                if slot.is_none() {
                    *slot = Some(err);
                }
            }
        });
    }
}

impl Reporter for PyReporter {
    fn build_begin(&self, plan: &ExecutionPlan) {
        self.emit(json!({
            "event": "build_begin",
            "plan": {"nodes": plan.nodes.iter().map(node_json).collect::<Vec<_>>()},
        }));
    }

    fn node_started(&self, node: &Node) {
        self.emit(json!({"event": "node_started", "node": node_json(node)}));
    }

    fn process_started(&self, node: &Node) {
        self.emit(json!({"event": "process_started", "node": node_json(node)}));
    }

    fn node_completed(
        &self,
        node: &Node,
        status: NodeState,
        result: Option<&ProcessResult>,
        error: Option<&str>,
    ) {
        self.emit(json!({
            "event": "node_completed",
            "node": node_json(node),
            "status": state_str(status),
            "result": result.map(|r| serde_json::to_value(r).expect("result serializes")),
            "error": error,
        }));
    }

    fn node_skipped(&self, node: &Node, why: &str) {
        self.emit(json!({"event": "node_skipped", "node": node_json(node), "why": why}));
    }

    fn collection_realized(&self, node: &Node, members: &[String], children: &[&Node]) {
        self.emit(json!({
            "event": "collection_realized",
            "node": node_json(node),
            "members": members,
            "children": children.iter().map(|c| node_json(c)).collect::<Vec<_>>(),
        }));
    }

    fn node_inserted(&self, node: &Node, reason: &str) {
        self.emit(json!({"event": "node_inserted", "node": node_json(node), "reason": reason}));
    }

    fn build_end(&self, _result: &BuildResult) {
        // The full result (final plan included) needs the plan, which the
        // core hands back from `build()` itself — the Python side synthesizes
        // its `build_end` reporter call from the returned result, so nothing
        // is emitted here. (Emitting a plan-less result would just be a
        // second, weaker copy of the return value.)
    }
}

/// The layer stack from the config's `layers` list (innermost-first, like
/// Python's `layers()`). The cache layer shares the incremental layer's
/// detector when one is configured — the same wiring as `CacheBuilder`.
fn build_layers(config: &Value, root: &str) -> Result<Vec<Box<dyn BuildLayer>>, String> {
    let empty = Vec::new();
    let configs = match config.get("layers") {
        None | Some(Value::Null) => &empty,
        Some(Value::Array(list)) => list,
        Some(other) => return Err(format!("config.layers must be a list, got {other}")),
    };
    // The incremental layer first (whatever its position), so the cache
    // layer can share its detector.
    let mut incremental: Option<IncrementalLayer> = None;
    for entry in configs {
        if entry.get("kind").and_then(|k| k.as_str()) == Some("incremental") {
            let dir = entry
                .get("metadata_dir")
                .and_then(|d| d.as_str())
                .unwrap_or("build/.brix");
            let metadata =
                std::sync::Arc::new(BuildMetadata::with_directory(root, dir));
            metadata.load();
            incremental = Some(IncrementalLayer::new(metadata));
        }
    }
    let mut layers: Vec<Box<dyn BuildLayer>> = Vec::new();
    for entry in configs {
        match entry.get("kind").and_then(|k| k.as_str()) {
            Some("incremental") => {
                layers.push(Box::new(incremental.take().ok_or_else(|| {
                    "config.layers lists incremental twice".to_string()
                })?));
            }
            Some("cache") => {
                let dir = entry
                    .get("cache_dir")
                    .and_then(|d| d.as_str())
                    .unwrap_or("build/.brix-cache");
                let detector = incremental
                    .as_ref()
                    .map(|layer| layer.detector.clone())
                    .unwrap_or_default();
                let resolved =
                    brix_core::cache::resolve_cache_dir(std::path::Path::new(root), dir);
                layers.push(Box::new(CacheLayer::new(
                    Box::new(CacheStore::new(resolved)),
                    detector,
                )));
            }
            Some("atomic") => {
                let dir = entry
                    .get("staging_dir")
                    .and_then(|d| d.as_str())
                    .unwrap_or("build/staging");
                let hold = entry.get("hold").and_then(|h| h.as_bool()).unwrap_or(false);
                layers.push(Box::new(AtomicLayer::new(dir, hold)));
            }
            other => return Err(format!("unknown layer kind: {other:?}")),
        }
    }
    Ok(layers)
}

/// Run one build. `spec_json` and `config_json` are the wire shapes from
/// `docs/boundary-schema.md`; the return value is the BuildResult JSON
/// (final plan included). `on_event` receives each event as a JSON string.
#[pyfunction]
#[pyo3(signature = (spec_json, config_json, on_event=None))]
fn build(
    py: Python<'_>,
    spec_json: &str,
    config_json: &str,
    on_event: Option<Py<PyAny>>,
) -> PyResult<String> {
    let config: Value = serde_json::from_str(config_json)
        .map_err(|e| PyValueError::new_err(format!("bad config: {e}")))?;
    let root = config
        .get("root")
        .and_then(|r| r.as_str())
        .ok_or_else(|| PyValueError::new_err("config.root is required"))?
        .to_string();
    let jobs = config.get("jobs").and_then(|j| j.as_u64()).map(|j| j as usize);
    let targets: Option<Vec<String>> = config.get("targets").and_then(|t| t.as_array()).map(|l| {
        l.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()
    });

    let spec = brix_core::spec::parse(spec_json).map_err(PyValueError::new_err)?;
    let mut plan = brix_core::planner::plan_graph(spec.actions, &root, targets.as_deref())
        .map_err(PyValueError::new_err)?;
    let layers = build_layers(&config, &root).map_err(PyValueError::new_err)?;
    let layer_refs: Vec<&dyn BuildLayer> = layers.iter().map(|b| b.as_ref()).collect();

    let reporter: Option<PyReporter> =
        on_event.map(|callback| PyReporter { callback, error: Mutex::new(None) });

    let result = py.detach(|| match &reporter {
        Some(bridge) => execute(&mut plan, jobs, &layer_refs, bridge),
        None => execute(&mut plan, jobs, &layer_refs, &NullReporter),
    });

    if let Some(bridge) = reporter {
        if let Some(err) = bridge.error.into_inner().unwrap() {
            return Err(err);
        }
    }
    Ok(result_json(&result, &plan).to_string())
}

#[pymodule]
fn brix_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(build, m)?)?;
    m.add("__engine__", "rust")?;
    Ok(())
}
