//! Logical actions — the nodes of the graph spec. Mirrors
//! `src/brix/actions.py` and the wire `Action` shape in
//! `docs/boundary-schema.md` (Process | Expand | Delete).
//!
//! `inputs`/`outputs`/`env` keep **insertion order** (a `Vec` of pairs, not a
//! sorted map): dependency inference reports the *first* overlapping atom's
//! text as the edge reason, so iteration order is observable in
//! `plan.describe()` and must match Python's ordered dicts.

use std::fmt;

use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer};

use crate::data::{DataAtom, DataExpr};

/// How to launch a persistent worker (mirrors `WorkerSpec` in
/// `src/brix/workers.py`): `command_line` renders from `inputs` into the
/// worker's launch argv exactly like an action's; `name` is diagnostic
/// identity. Purely an execution transport — a worker-routed action must be
/// indistinguishable from a spawned one except in speed.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct WorkerSpec {
    pub name: String,
    pub command_line: String,
    #[serde(default, deserialize_with = "ordered_map")]
    pub inputs: Vec<(String, DataExpr)>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Action {
    Process {
        #[serde(default)]
        name: Option<String>,
        command_line: String,
        #[serde(default, deserialize_with = "ordered_map")]
        inputs: Vec<(String, DataExpr)>,
        #[serde(default, deserialize_with = "ordered_map")]
        outputs: Vec<(String, DataExpr)>,
        #[serde(default, deserialize_with = "ordered_map")]
        env: Vec<(String, DataExpr)>,
        /// Environment variable names forced absent from the process environment
        /// (the `UNSET` sentinel). Part of `action_key` (identity), like env
        /// shapes; never substituted (they are plain variable names).
        #[serde(default)]
        env_unset: Vec<String>,
        /// Scrub-allowlist names the executor lets through the authoritative
        /// environment (resolved from its own ambient env). `env_used` are the
        /// SYSTEM-tracked names (fingerprinted via the snapshot input); `env_pass`
        /// are the PASS names (deliberately not fingerprinted). Neither is part of
        /// `action_key` — only used to build the scrub environment.
        #[serde(default)]
        env_pass: Vec<String>,
        #[serde(default)]
        env_used: Vec<String>,
        /// Workspace-relative path of a Makefile-format dependency file the
        /// tool writes (`cc -MMD -MF`); the incremental layer scans it after a
        /// successful run to learn undeclared inputs (C/C++ headers). Not
        /// identity: never part of `action_key`.
        #[serde(default)]
        depfile: Option<String>,
        /// A worker executes this action as a request instead of spawning it;
        /// None spawns a process (mirrors `ExternalProcessAction.worker`).
        #[serde(default)]
        worker: Option<WorkerSpec>,
    },
    Expand {
        element: String,
        collection: DataExpr,
        child: Box<Action>,
    },
    Delete {
        paths: Vec<String>,
    },
}

/// Deserialize a JSON object into `Vec<(String, DataExpr)>` preserving the
/// document's key order (serde's `Vec` alone would demand a JSON array).
fn ordered_map<'de, D>(d: D) -> Result<Vec<(String, DataExpr)>, D::Error>
where
    D: Deserializer<'de>,
{
    struct OrderedVisitor;
    impl<'de> Visitor<'de> for OrderedVisitor {
        type Value = Vec<(String, DataExpr)>;
        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
            f.write_str("a JSON object of name -> data expression")
        }
        fn visit_map<M>(self, mut map: M) -> Result<Self::Value, M::Error>
        where
            M: MapAccess<'de>,
        {
            let mut out = Vec::new();
            while let Some((k, v)) = map.next_entry::<String, DataExpr>()? {
                out.push((k, v));
            }
            Ok(out)
        }
    }
    d.deserialize_map(OrderedVisitor)
}

impl Action {
    pub fn label(&self) -> String {
        match self {
            Action::Process { name, command_line, .. } => {
                name.clone().unwrap_or_else(|| command_line.clone())
            }
            Action::Expand { child, .. } => format!("expand: {}", child.label()),
            Action::Delete { paths } => format!("rm {}", paths.join(" ")),
        }
    }

    /// Anything an `Expand` can instantiate per member: Process or Expand,
    /// never Delete.
    pub fn is_expandable(&self) -> bool {
        !matches!(self, Action::Delete { .. })
    }

    /// Path atoms among the action's inputs (drives dependency inference and
    /// target-selection needs). A worker's own inputs (e.g. a bootstrap-built
    /// worker jar) are inputs this action needs present, so they create
    /// dependency edges like any other input (mirrors `planner.input_atoms`).
    pub fn input_atoms(&self) -> Vec<DataAtom> {
        match self {
            Action::Process { inputs, worker, .. } => {
                let mut atoms = path_atoms(inputs);
                if let Some(spec) = worker {
                    atoms.extend(path_atoms(&spec.inputs));
                }
                atoms
            }
            Action::Expand { collection, child, .. } => {
                let mut atoms: Vec<DataAtom> =
                    collection.iter().filter(|a| a.is_path_atom()).cloned().collect();
                atoms.extend(child.input_atoms());
                atoms
            }
            Action::Delete { .. } => Vec::new(),
        }
    }

    pub fn output_atoms(&self) -> Vec<DataAtom> {
        match self {
            Action::Process { outputs, .. } => path_atoms(outputs),
            Action::Expand { child, .. } => child.output_atoms(),
            Action::Delete { .. } => Vec::new(),
        }
    }

    /// Apply `{name}` bindings. An `Expand` whose own `element` is bound
    /// collapses to its substituted child (there is nothing left to expand
    /// over); otherwise it substitutes its collection and child and keeps the
    /// Expand shape. Mirrors `ExpandAll.substitute`.
    pub fn substitute(&self, bindings: &[(String, String)]) -> Action {
        match self {
            Action::Process { name, command_line, inputs, outputs, env, env_unset, env_pass, env_used, depfile, worker } => {
                // `name` and `depfile` substitute by plain `{key}` -> value
                // string replacement (mirrors `ExternalProcessAction.substitute`).
                let replace_keys = |s: &str| {
                    let mut s = s.to_string();
                    for (k, v) in bindings {
                        s = s.replace(&format!("{{{k}}}"), v);
                    }
                    s
                };
                Action::Process {
                    name: name.as_deref().map(replace_keys),
                    command_line: command_line.clone(),
                    inputs: subst_pairs(inputs, bindings),
                    outputs: subst_pairs(outputs, bindings),
                    env: subst_pairs(env, bindings),
                    env_unset: env_unset.clone(),
                    env_pass: env_pass.clone(),
                    env_used: env_used.clone(),
                    depfile: depfile.as_deref().map(replace_keys),
                    // The worker passes through untouched (a shared spec, not
                    // per-member data) — mirrors Python's `worker=self.worker`.
                    worker: worker.clone(),
                }
            }
            Action::Expand { element, collection, child } => {
                if bindings.iter().any(|(k, _)| k == element) {
                    child.substitute(bindings)
                } else {
                    Action::Expand {
                        element: element.clone(),
                        collection: collection.iter().map(|a| a.substitute(bindings)).collect(),
                        child: Box::new(child.substitute(bindings)),
                    }
                }
            }
            Action::Delete { .. } => self.clone(),
        }
    }
}

fn path_atoms(pairs: &[(String, DataExpr)]) -> Vec<DataAtom> {
    pairs
        .iter()
        .flat_map(|(_, expr)| expr.iter())
        .filter(|a| a.is_path_atom())
        .cloned()
        .collect()
}

fn subst_pairs(
    pairs: &[(String, DataExpr)],
    bindings: &[(String, String)],
) -> Vec<(String, DataExpr)> {
    pairs
        .iter()
        .map(|(k, expr)| (k.clone(), expr.iter().map(|a| a.substitute(bindings)).collect()))
        .collect()
}
