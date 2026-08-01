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
use pyo3::types::PyBytes;
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

/// A JSON value as the equivalent Python object (via the `json` module — no
/// extra conversion dependency; these cross rarely, not per-file).
fn to_py_json<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    py.import("json")?.call_method1("loads", (value.to_string(),))
}

/// A Python object as a JSON value. Errs on non-JSON-able objects — a
/// custom store's entries must be plain data (documented in
/// docs/boundary-schema.md); the cache layer treats the error as a miss.
fn from_py_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let text: String = py.import("json")?.call_method1("dumps", (obj,))?.extract()?;
    serde_json::from_str(&text).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// The skin's custom content-identity plugins: `name -> (tag, digest)`
/// resolved by the skin (Python's `identity` registry), with each `digest`
/// a callable invoked per file across the boundary. A raising digest maps
/// to `Err`, which the engine turns into the `tag:raw` byte-hash fallback —
/// the same contract a Python-engine plugin has.
struct PyCustomIdentities {
    map: std::collections::HashMap<String, (String, Py<PyAny>)>,
}

impl brix_core::content_key::CustomIdentities for PyCustomIdentities {
    fn tag(&self, name: &str) -> Option<String> {
        self.map.get(name).map(|(tag, _)| tag.clone())
    }

    fn digest(&self, name: &str, path: &std::path::Path) -> Result<String, String> {
        let (_, digest) = self.map.get(name).ok_or_else(|| format!("unknown identity {name}"))?;
        Python::attach(|py| {
            digest
                .call1(py, (path.to_string_lossy(),))
                .and_then(|obj| obj.extract::<String>(py))
                .map_err(|e| e.to_string())
        })
    }
}

/// A skin cache store behind the wire call shapes (`load` / `restore` /
/// `store`, docs/boundary-schema.md). Entries cross as JSON; the store
/// object itself is the skin-side shim (`engine._StoreShim`), which adapts
/// these shapes back to the user's `CacheStoreProtocol` object. Any Python
/// exception becomes `Err`, which the cache layer treats as a miss or a
/// skipped populate — same resilience as a Python-engine store.
struct PyCacheStore {
    store: Py<PyAny>,
}

impl brix_core::cache::CacheStoreProtocol for PyCacheStore {
    fn load(&self, fingerprint: &str) -> Result<Option<brix_core::cache::Entry>, String> {
        Python::attach(|py| {
            let entry = self
                .store
                .call_method1(py, "load", (fingerprint,))
                .map_err(|e| e.to_string())?;
            if entry.is_none(py) {
                return Ok(None);
            }
            from_py_json(py, entry.bind(py)).map(Some).map_err(|e| e.to_string())
        })
    }

    fn restore(
        &self,
        fingerprint: &str,
        entry: &brix_core::cache::Entry,
        ctx: &brix_core::NodeContext,
    ) -> Result<bool, String> {
        Python::attach(|py| {
            let entry_obj = to_py_json(py, entry).map_err(|e| e.to_string())?;
            let output_root = ctx.output_root.to_string_lossy();
            self.store
                .call_method1(py, "restore", (fingerprint, entry_obj, output_root))
                .and_then(|ok| ok.extract::<bool>(py))
                .map_err(|e| e.to_string())
        })
    }

    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &std::collections::BTreeMap<String, std::path::PathBuf>,
    ) -> Result<(), String> {
        Python::attach(|py| {
            let result_obj = to_py_json(
                py,
                &serde_json::to_value(result).expect("result serializes"),
            )
            .map_err(|e| e.to_string())?;
            let outputs_obj: std::collections::BTreeMap<String, String> = outputs
                .iter()
                .map(|(logical, physical)| {
                    (logical.clone(), physical.to_string_lossy().to_string())
                })
                .collect();
            self.store
                .call_method1(py, "store", (fingerprint, result_obj, outputs_obj))
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
    }
}

type IdentityMap = std::collections::HashMap<String, (String, Py<PyAny>)>;

fn custom_identities(
    identities: Option<IdentityMap>,
) -> Option<std::sync::Arc<dyn brix_core::content_key::CustomIdentities>> {
    identities.map(|map| {
        std::sync::Arc::new(PyCustomIdentities { map })
            as std::sync::Arc<dyn brix_core::content_key::CustomIdentities>
    })
}

/// The atomic layer must be outermost — the last entry, since `config.layers`
/// is innermost-first. This is the language-neutral form of "AtomicBuilder must
/// be the outermost builder" (docs/boundary-schema.md): validating it in the
/// shared core means every frontend inherits the rule instead of reimplementing
/// it. (The Python builder types also express it, so an IDE flags a bad stack
/// before the spec is ever built.)
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
/// detector when one is configured — the same wiring as `CacheBuilder`.
/// `stores[i]`, when set, is the skin store shim backing layer `i`;
/// `custom` is the skin's `content_identity` extension, injected into every
/// detector so fingerprints resolve custom identity names.
fn build_layers(
    config: &Value,
    root: &str,
    stores: &[Option<Py<PyAny>>],
    custom: Option<std::sync::Arc<dyn brix_core::content_key::CustomIdentities>>,
) -> Result<Vec<Box<dyn BuildLayer>>, String> {
    let empty = Vec::new();
    let configs = match config.get("layers") {
        None | Some(Value::Null) => &empty,
        Some(Value::Array(list)) => list,
        Some(other) => return Err(format!("config.layers must be a list, got {other}")),
    };
    validate_atomic_outermost(configs)?;
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
            // Opt-in git acceleration (the Python side sets this flag when a
            // GitChangeDetector is configured). `prepare` records the next
            // baseline and returns a vouch only when the repo/baseline are
            // ready; otherwise the detector runs the plain stat scan.
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
                .with_custom_identities(custom.clone())
                .with_git(git)
                .with_verify_contents(verify_contents);
            incremental = Some(IncrementalLayer::with_detector(metadata, detector));
        }
    }
    let mut layers: Vec<Box<dyn BuildLayer>> = Vec::new();
    for (index, entry) in configs.iter().enumerate() {
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
                    .unwrap_or_else(|| {
                        brix_core::TimeAndHashChangeDetector::default()
                            .with_custom_identities(custom.clone())
                    });
                let store: Box<dyn brix_core::cache::CacheStoreProtocol> =
                    match stores.get(index).and_then(|s| s.as_ref()) {
                        Some(shim) => Box::new(PyCacheStore {
                            store: Python::attach(|py| shim.clone_ref(py)),
                        }),
                        None => {
                            let dir = entry
                                .get("cache_dir")
                                .and_then(|d| d.as_str())
                                .unwrap_or("build/.brix-cache");
                            let resolved = brix_core::cache::resolve_cache_dir(
                                std::path::Path::new(root),
                                dir,
                            );
                            Box::new(CacheStore::new(resolved))
                        }
                    };
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

/// Run one build. `spec_json` and `config_json` are the wire shapes from
/// `docs/boundary-schema.md`; the return value is the BuildResult JSON
/// (final plan included). `on_event` receives each event as a JSON string.
/// `stores` (aligned with `config.layers`) carries skin cache-store shims;
/// `identities` is the skin's `content_identity` extension as
/// `{name: (tag, digest_callable)}` — both per the extension-trait shapes.
#[pyfunction]
#[pyo3(signature = (spec_json, config_json, on_event=None, stores=None, identities=None))]
fn build(
    py: Python<'_>,
    spec_json: &str,
    config_json: &str,
    on_event: Option<Py<PyAny>>,
    stores: Option<Vec<Option<Py<PyAny>>>>,
    identities: Option<IdentityMap>,
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
    let mut plan = brix_core::planner::plan_graph_with_orders(
        spec.actions,
        &spec.forced_orders,
        &root,
        targets.as_deref(),
    )
    .map_err(PyValueError::new_err)?;
    let custom = custom_identities(identities);
    let layers = build_layers(&config, &root, &stores.unwrap_or_default(), custom.clone())
        .map_err(PyValueError::new_err)?;
    let layer_refs: Vec<&dyn BuildLayer> = layers.iter().map(|b| b.as_ref()).collect();
    // Scrub is a build-wide policy carried on the incremental layer spec.
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

    // Cross-build worker daemons (docs/java-fast-builds.md v2): one knob.
    // 0 = no persistence (v1 pipes, terminate at build end). The registry
    // lives under the metadata dir; a missing brix-workerd binary is only
    // an error when a worker-routed action actually runs — loud there,
    // invisible to builds that use no workers.
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
            detector: brix_core::TimeAndHashChangeDetector::default()
                .with_custom_identities(custom),
        })
    };

    let reporter: Option<PyReporter> =
        on_event.map(|callback| PyReporter { callback, error: Mutex::new(None) });

    let result = py.detach(|| match &reporter {
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
    });

    if let Some(bridge) = reporter {
        if let Some(err) = bridge.error.into_inner().unwrap() {
            return Err(err);
        }
    }
    Ok(result_json(&result, &plan).to_string())
}

/// The parsed prologue of a no-build entry point: the spec's actions plus
/// the config's root, directory knobs, and targets.
struct InspectConfig {
    actions: Vec<brix_core::Action>,
    forced_orders: Vec<brix_core::spec::ForcedOrder>,
    root: String,
    metadata_dir: String,
    staging_dir: String,
    targets: Option<Vec<String>>,
}

fn parse_inspect_config(spec_json: &str, config_json: &str) -> PyResult<InspectConfig> {
    let config: Value = serde_json::from_str(config_json)
        .map_err(|e| PyValueError::new_err(format!("bad config: {e}")))?;
    let root = config
        .get("root")
        .and_then(|r| r.as_str())
        .ok_or_else(|| PyValueError::new_err("config.root is required"))?
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
    let spec = brix_core::spec::parse(spec_json).map_err(PyValueError::new_err)?;
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
/// targets?}`. Raises ValueError where Python's `compute_status` raises
/// (bad targets, unknown identity).
#[pyfunction]
#[pyo3(signature = (spec_json, config_json, identities=None))]
fn status(
    py: Python<'_>,
    spec_json: &str,
    config_json: &str,
    identities: Option<IdentityMap>,
) -> PyResult<String> {
    let cfg = parse_inspect_config(spec_json, config_json)?;
    let custom = custom_identities(identities);
    let report = py
        .detach(|| {
            brix_core::status::compute_status_with_orders(
                cfg.actions,
                &cfg.forced_orders,
                std::path::Path::new(&cfg.root),
                &cfg.metadata_dir,
                &cfg.staging_dir,
                cfg.targets.as_deref(),
                custom,
            )
        })
        .map_err(PyValueError::new_err)?;
    Ok(brix_core::status::report_to_json(&report))
}

/// Delete declared and recorded outputs plus the metadata store; returns
/// `{"removed": n}`. Config: `{root, metadata_dir?}`.
#[pyfunction]
fn clean(py: Python<'_>, spec_json: &str, config_json: &str) -> PyResult<String> {
    let cfg = parse_inspect_config(spec_json, config_json)?;
    let removed = py
        .detach(|| {
            brix_core::status::clean(
                cfg.actions,
                std::path::Path::new(&cfg.root),
                &cfg.metadata_dir,
            )
        })
        .map_err(PyValueError::new_err)?;
    Ok(json!({ "removed": removed }).to_string())
}

/// Console-script entry for the bundled cross-build worker holder. The Rust
/// daemon parses this process's argv and detaches exactly like the standalone
/// development binary; the GIL must not remain held across its serve loop.
#[pyfunction]
fn workerd_main(py: Python<'_>) -> PyResult<()> {
    let argv: Vec<String> = py.import("sys")?.getattr("argv")?.extract()?;
    py.detach(|| brix_workerd::run_args(argv.into_iter().skip(1).collect()));
    Ok(())
}

/// The content digest (sha256 hex of the bytes) of a brix-shipped asset,
/// resolved from the copy embedded in this extension. Lets the Python asset
/// store serve helpers from the engine when no on-disk `assets/` tree is present
/// (an installed wheel) — the engine owns the bytes (docs/asset-engine.md).
#[pyfunction]
fn asset_digest(name: &str) -> PyResult<String> {
    brix_core::assets::digest(name).map_err(PyValueError::new_err)
}

/// The bytes of a brix-shipped asset, from the copy embedded in this extension.
#[pyfunction]
fn asset_bytes<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyBytes>> {
    match brix_core::assets::bytes(name) {
        Some(b) => Ok(PyBytes::new(py, b)),
        None => Err(PyValueError::new_err(format!("asset not shipped: {name}"))),
    }
}

#[pymodule]
fn brix_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(build, m)?)?;
    m.add_function(wrap_pyfunction!(status, m)?)?;
    m.add_function(wrap_pyfunction!(clean, m)?)?;
    m.add_function(wrap_pyfunction!(workerd_main, m)?)?;
    m.add_function(wrap_pyfunction!(asset_digest, m)?)?;
    m.add_function(wrap_pyfunction!(asset_bytes, m)?)?;
    m.add("__engine__", "rust")?;
    Ok(())
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
    fn no_atomic_is_accepted() {
        let configs = vec![json!({"kind": "cache"}), json!({"kind": "incremental"})];
        assert!(validate_atomic_outermost(&configs).is_ok());
    }

    #[test]
    fn atomic_not_last_is_rejected() {
        // atomic buried before cache: the malformed spec a hand-built or future
        // frontend might emit. The Python builders can't produce this, but the
        // core is the shared backstop for every frontend.
        let configs = vec![
            json!({"kind": "atomic"}),
            json!({"kind": "incremental"}),
        ];
        let err = validate_atomic_outermost(&configs).unwrap_err();
        assert!(err.contains("outermost"), "{err}");
    }
}
