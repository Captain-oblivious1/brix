//! The N-API boundary: `brix_node.build/status/clean(specJson, configJson, ...)`.
//!
//! The Node twin of `brix-py`. Same three JSON strings, same `brix-core`
//! engine — only the FFI prologue differs (N-API where brix-py has PyO3).
//! The graph spec and build config go in (shapes pinned by
//! `docs/boundary-schema.md`), the build result comes back as JSON.
//!
//! Threading — the one real difference from brix-py. Python re-enters its
//! callback synchronously on any thread by acquiring the GIL; a JS engine is
//! single-threaded, so a worker thread cannot call JS directly. The resolution
//! is boundary-schema deviation #2: **`build` is async** (returns a Promise).
//! The heavy build runs on a libuv worker thread via `AsyncTask`, which frees
//! the JS event loop to service the event callback. Each event crosses through
//! a `ThreadsafeFunction` in non-blocking mode (order-preserving), so the
//! reporter sees live progress exactly as the Python skin does. `status`/`clean`
//! stay synchronous — they run no processes and emit no events.
//!
//! Deferred (an optional extension trait, not needed to author or run a build):
//! custom cache-store / content-identity callbacks. Those need a *blocking*
//! call into JS that returns a value to a Rust worker thread — the genuinely
//! hard case reserved for review — so they land after the core mirror is proven.

use napi::bindgen_prelude::AsyncTask;
use napi::threadsafe_function::{ThreadsafeFunction, ThreadsafeFunctionCallMode};
use napi::{Env, Status, Task};
use napi_derive::napi;
use serde_json::{json, Map, Value};

use brix_core::atomic::AtomicLayer;
use brix_core::cache::{CacheLayer, CacheStore};
use brix_core::executor::BuildResult;
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

/// The event callback as a threadsafe function: `String` in (the event JSON),
/// nothing back, delivered as the JS function's single argument
/// (`CalleeHandled = false`, so no leading Node-style error arg). Non-blocking
/// calls from worker threads, order-preserving.
type EventTsfn = ThreadsafeFunction<String, (), String, Status, false>;

/// Bridges core events to the JS callback as JSON strings. Mirrors brix-py's
/// `PyReporter`, one event object per call (the shapes of
/// `docs/boundary-schema.md`). A dropped/failed enqueue is not fatal — the
/// build cannot be stopped mid-flight, same as the Python skin.
struct NodeReporter {
    tsfn: EventTsfn,
}

impl NodeReporter {
    fn emit(&self, event: Value) {
        let _ = self.tsfn.call(event.to_string(), ThreadsafeFunctionCallMode::NonBlocking);
    }
}

impl Reporter for NodeReporter {
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
        // The full result (final plan included) comes back from the Promise;
        // the skin synthesizes its build_end reporter call from it, same as
        // brix-py. Emitting a plan-less result here would just be a weaker copy.
    }
}

/// The atomic layer must be outermost — the last entry, since `config.layers`
/// is innermost-first (the language-neutral form of "AtomicBuilder must be the
/// outermost builder", docs/boundary-schema.md). Validated in every frontend's
/// binding so the rule is inherited, not reimplemented.
fn validate_atomic_outermost(configs: &[Value]) -> Result<(), String> {
    for (index, entry) in configs.iter().enumerate() {
        if entry.get("kind").and_then(|k| k.as_str()) == Some("atomic")
            && index != configs.len() - 1
        {
            return Err(
                "the atomic layer must be outermost (the last entry in \
                 config.layers); it was wrapped by another layer"
                    .to_string(),
            );
        }
    }
    Ok(())
}

/// The layer stack from the config's `layers` list (innermost-first, like
/// Python's `layers()`). The cache layer shares the incremental layer's
/// detector when one is configured. This milestone uses the default filesystem
/// cache store and no custom identities.
fn build_layers(config: &Value, root: &str) -> Result<Vec<Box<dyn BuildLayer>>, String> {
    let empty = Vec::new();
    let configs = match config.get("layers") {
        None | Some(Value::Null) => &empty,
        Some(Value::Array(list)) => list,
        Some(other) => return Err(format!("config.layers must be a list, got {other}")),
    };
    validate_atomic_outermost(configs)?;
    // The incremental layer first (whatever its position), so the cache layer
    // can share its detector.
    let mut incremental: Option<IncrementalLayer> = None;
    for entry in configs {
        if entry.get("kind").and_then(|k| k.as_str()) == Some("incremental") {
            let dir = entry
                .get("metadata_dir")
                .and_then(|d| d.as_str())
                .unwrap_or("build/.brix");
            let metadata = std::sync::Arc::new(BuildMetadata::with_directory(root, dir));
            metadata.load();
            let git = if entry.get("git_detection").and_then(|v| v.as_bool()).unwrap_or(false) {
                brix_core::gitdetect::prepare(std::path::Path::new(root), &metadata)
                    .map(std::sync::Arc::new)
            } else {
                None
            };
            let verify_contents = entry
                .get("verify_contents")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let detector = brix_core::TimeAndHashChangeDetector::with_metadata(metadata.clone())
                .with_git(git)
                .with_verify_contents(verify_contents);
            incremental = Some(IncrementalLayer::with_detector(metadata, detector));
        }
    }
    let mut layers: Vec<Box<dyn BuildLayer>> = Vec::new();
    for entry in configs.iter() {
        match entry.get("kind").and_then(|k| k.as_str()) {
            Some("incremental") => {
                layers.push(Box::new(incremental.take().ok_or_else(|| {
                    "config.layers lists incremental twice".to_string()
                })?));
            }
            Some("cache") => {
                let detector = incremental
                    .as_ref()
                    .map(|layer| layer.detector.clone())
                    .unwrap_or_else(brix_core::TimeAndHashChangeDetector::default);
                let dir = entry
                    .get("cache_dir")
                    .and_then(|d| d.as_str())
                    .unwrap_or("build/.brix-cache");
                let resolved =
                    brix_core::cache::resolve_cache_dir(std::path::Path::new(root), dir);
                let store: Box<dyn brix_core::cache::CacheStoreProtocol> =
                    Box::new(CacheStore::new(resolved));
                layers.push(Box::new(CacheLayer::new(store, detector)));
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

fn err(message: String) -> napi::Error {
    napi::Error::from_reason(message)
}

/// The build body, shared by the sync and async paths. `reporter` is the live
/// event bridge when the caller passed an `on_event` callback.
fn run_build(
    spec_json: &str,
    config_json: &str,
    reporter: Option<&NodeReporter>,
) -> napi::Result<String> {
    let config: Value = serde_json::from_str(config_json)
        .map_err(|e| err(format!("bad config: {e}")))?;
    let root = config
        .get("root")
        .and_then(|r| r.as_str())
        .ok_or_else(|| err("config.root is required".to_string()))?
        .to_string();
    let jobs = config.get("jobs").and_then(|j| j.as_u64()).map(|j| j as usize);
    let targets: Option<Vec<String>> = config.get("targets").and_then(|t| t.as_array()).map(|l| {
        l.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()
    });

    let spec = brix_core::spec::parse(spec_json).map_err(err)?;
    let mut plan = brix_core::planner::plan_graph_with_orders(
        spec.actions,
        &spec.forced_orders,
        &root,
        targets.as_deref(),
    )
    .map_err(err)?;
    let layers = build_layers(&config, &root).map_err(err)?;
    let layer_refs: Vec<&dyn BuildLayer> = layers.iter().map(|b| b.as_ref()).collect();
    let scrub = config
        .get("layers")
        .and_then(|v| v.as_array())
        .map(|entries| {
            entries.iter().any(|e| {
                e.get("kind").and_then(|k| k.as_str()) == Some("incremental")
                    && e.get("scrub").and_then(|s| s.as_bool()).unwrap_or(false)
            })
        })
        .unwrap_or(false);

    let idle_timeout =
        config.get("worker_idle_timeout").and_then(|v| v.as_u64()).unwrap_or(900);
    let workers = if idle_timeout == 0 {
        brix_core::WorkerRegistry::new()
    } else {
        let metadata_dir = config
            .get("layers")
            .and_then(|l| l.as_array())
            .and_then(|layers| {
                layers.iter().find_map(|entry| {
                    (entry.get("kind").and_then(|k| k.as_str()) == Some("incremental"))
                        .then(|| entry.get("metadata_dir").and_then(|d| d.as_str()))
                        .flatten()
                })
            })
            .unwrap_or("build/.brix");
        let workerd = config
            .get("workerd")
            .and_then(|w| w.as_str())
            .unwrap_or("brix-workerd")
            .to_string();
        brix_core::WorkerRegistry::with_daemon(brix_core::DaemonConfig {
            registry_dir: std::path::Path::new(&root).join(metadata_dir).join("workers"),
            idle_timeout,
            workerd: workerd.into(),
            detector: brix_core::TimeAndHashChangeDetector::default(),
        })
    };

    let result = match reporter {
        Some(bridge) => {
            brix_core::execute_with_workers(&mut plan, jobs, &layer_refs, bridge, workers, scrub)
        }
        None => brix_core::execute_with_workers(
            &mut plan,
            jobs,
            &layer_refs,
            &NullReporter,
            workers,
            scrub,
        ),
    };
    Ok(result_json(&result, &plan).to_string())
}

/// The async build task: `compute` runs `run_build` on a libuv worker thread
/// (freeing the JS loop to service event callbacks); `resolve` hands the
/// BuildResult JSON back as the resolved Promise value.
pub struct BuildTask {
    spec_json: String,
    config_json: String,
    reporter: Option<NodeReporter>,
}

impl Task for BuildTask {
    type Output = String;
    type JsValue = String;

    fn compute(&mut self) -> napi::Result<String> {
        run_build(&self.spec_json, &self.config_json, self.reporter.as_ref())
    }

    fn resolve(&mut self, _env: Env, output: String) -> napi::Result<String> {
        Ok(output)
    }
}

/// Run one build. `spec_json` and `config_json` are the wire shapes from
/// `docs/boundary-schema.md`; the Promise resolves to the BuildResult JSON
/// (final plan included). `on_event`, if given, receives each event as a JSON
/// string, live, in scheduling order (`process_started` may interleave, per
/// boundary-schema).
#[napi(ts_return_type = "Promise<string>")]
pub fn build(
    spec_json: String,
    config_json: String,
    on_event: Option<EventTsfn>,
) -> AsyncTask<BuildTask> {
    let reporter = on_event.map(|tsfn| NodeReporter { tsfn });
    AsyncTask::new(BuildTask { spec_json, config_json, reporter })
}

/// The parsed prologue of a no-build entry point: the spec's actions plus the
/// config's root, directory knobs, and targets.
struct InspectConfig {
    actions: Vec<brix_core::Action>,
    forced_orders: Vec<brix_core::spec::ForcedOrder>,
    root: String,
    metadata_dir: String,
    staging_dir: String,
    targets: Option<Vec<String>>,
}

fn parse_inspect_config(spec_json: &str, config_json: &str) -> napi::Result<InspectConfig> {
    let config: Value = serde_json::from_str(config_json)
        .map_err(|e| err(format!("bad config: {e}")))?;
    let root = config
        .get("root")
        .and_then(|r| r.as_str())
        .ok_or_else(|| err("config.root is required".to_string()))?
        .to_string();
    let metadata_dir = config
        .get("metadata_dir")
        .and_then(|d| d.as_str())
        .unwrap_or("build/.brix")
        .to_string();
    let staging_dir = config
        .get("staging_dir")
        .and_then(|d| d.as_str())
        .unwrap_or("build/staging")
        .to_string();
    let targets: Option<Vec<String>> = config.get("targets").and_then(|t| t.as_array()).map(|l| {
        l.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()
    });
    let spec = brix_core::spec::parse(spec_json).map_err(err)?;
    Ok(InspectConfig {
        actions: spec.actions,
        forced_orders: spec.forced_orders,
        root,
        metadata_dir,
        staging_dir,
        targets,
    })
}

/// Build-state inspection without building: the StatusReport JSON of
/// `docs/boundary-schema.md`. Config: `{root, metadata_dir?, staging_dir?,
/// targets?}`.
#[napi]
pub fn status(spec_json: String, config_json: String) -> napi::Result<String> {
    let cfg = parse_inspect_config(&spec_json, &config_json)?;
    let report = brix_core::status::compute_status_with_orders(
        cfg.actions,
        &cfg.forced_orders,
        std::path::Path::new(&cfg.root),
        &cfg.metadata_dir,
        &cfg.staging_dir,
        cfg.targets.as_deref(),
        None,
    )
    .map_err(err)?;
    Ok(brix_core::status::report_to_json(&report))
}

/// Delete declared and recorded outputs plus the metadata store; returns
/// `{"removed": n}`. Config: `{root, metadata_dir?}`.
#[napi]
pub fn clean(spec_json: String, config_json: String) -> napi::Result<String> {
    let cfg = parse_inspect_config(&spec_json, &config_json)?;
    let removed = brix_core::status::clean(
        cfg.actions,
        std::path::Path::new(&cfg.root),
        &cfg.metadata_dir,
    )
    .map_err(err)?;
    Ok(json!({ "removed": removed }).to_string())
}

#[cfg(test)]
mod tests {
    use super::validate_atomic_outermost;
    use serde_json::json;

    #[test]
    fn atomic_last_is_accepted() {
        let configs = vec![
            json!({"kind": "cache"}),
            json!({"kind": "incremental"}),
            json!({"kind": "atomic"}),
        ];
        assert!(validate_atomic_outermost(&configs).is_ok());
    }

    #[test]
    fn atomic_not_last_is_rejected() {
        let configs = vec![json!({"kind": "atomic"}), json!({"kind": "incremental"})];
        let e = validate_atomic_outermost(&configs).unwrap_err();
        assert!(e.contains("outermost"), "{e}");
    }
}
