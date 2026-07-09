//! Persistent build metadata: the incremental layer's private storage.
//! Mirrors `BuildMetadata` in `src/brix/metadata.py`.
//!
//! One JSON file per workspace (default `build/.brix/metadata.json`) holding:
//! a file-hash cache keyed by absolute path (validated by `[mtime_ns, size]`),
//! one record per action (fingerprint, last result, output hashes), collection
//! membership from the previous build, and discovered (scanned) dependencies.
//!
//! **Interop is a hard requirement** (docs/rust-port.md): Rust must read
//! Python-written metadata and vice versa during the transition — same
//! schema, `"version": 1`, keys sorted on write (indent is cosmetic).
//! Records are held as raw JSON values and read leniently, exactly like
//! Python's `record.get(...)` chains: a malformed record degrades to "rerun",
//! never to a crash — and unknown keys (e.g. a `jvm-abi@1` identity digest
//! written by Python) survive a Rust load/save round trip untouched.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{json, Map, Value};

const VERSION: i64 = 1;

fn fresh() -> Map<String, Value> {
    let Value::Object(map) = json!({
        "version": VERSION,
        "files": {},
        "actions": {},
        "memberships": {},
        "deps": {},
    }) else {
        unreachable!()
    };
    map
}

/// Load/store for the metadata file. Thread-safe for concurrent nodes
/// (interior mutability; methods take `&self`).
pub struct BuildMetadata {
    pub root: PathBuf,
    pub directory: PathBuf,
    pub path: PathBuf,
    data: Mutex<Map<String, Value>>,
}

impl BuildMetadata {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self::with_directory(root, "build/.brix")
    }

    pub fn with_directory(root: impl Into<PathBuf>, directory: impl AsRef<Path>) -> Self {
        let root = root.into();
        let directory = root.join(directory.as_ref());
        let path = directory.join("metadata.json");
        BuildMetadata { root, directory, path, data: Mutex::new(fresh()) }
    }

    /// Read the file; any unreadable/unparsable/wrong-version content resets
    /// to fresh (mirrors Python's `load`).
    pub fn load(&self) {
        let loaded: Option<Map<String, Value>> = std::fs::read_to_string(&self.path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|v| match v {
                Value::Object(map) if map.get("version") == Some(&json!(VERSION)) => Some(map),
                _ => None,
            });
        *self.data.lock().unwrap() = loaded.unwrap_or_else(fresh);
    }

    /// Write-tmp-then-rename, keys sorted (serde_json's default object is a
    /// BTreeMap, so nested keys come out sorted like Python's `sort_keys`).
    pub fn save(&self) -> Result<(), String> {
        std::fs::create_dir_all(&self.directory)
            .map_err(|e| format!("cannot create metadata dir: {e}"))?;
        let text = {
            let data = self.data.lock().unwrap();
            serde_json::to_string_pretty(&Value::Object(data.clone()))
                .map_err(|e| format!("cannot serialize metadata: {e}"))?
        };
        let tmp = self.directory.join("metadata.json.tmp");
        std::fs::write(&tmp, text).map_err(|e| format!("cannot write metadata: {e}"))?;
        std::fs::rename(&tmp, &self.path).map_err(|e| format!("cannot commit metadata: {e}"))?;
        Ok(())
    }

    fn section<T>(&self, name: &str, f: impl FnOnce(&Map<String, Value>) -> T) -> T {
        let data = self.data.lock().unwrap();
        let empty = Map::new();
        let map = match data.get(name) {
            Some(Value::Object(map)) => map,
            _ => &empty,
        };
        f(map)
    }

    fn section_mut<T>(&self, name: &str, f: impl FnOnce(&mut Map<String, Value>) -> T) -> T {
        let mut data = self.data.lock().unwrap();
        let entry = data.entry(name.to_string()).or_insert_with(|| json!({}));
        match entry {
            Value::Object(map) => f(map),
            other => {
                *other = json!({});
                let Value::Object(map) = other else { unreachable!() };
                f(map)
            }
        }
    }

    // -- file-hash cache -----------------------------------------------------
    //
    // One entry per absolute path, validated by `[mtime_ns, size]`. It holds
    // the byte hash under "sha256" and any content-identity digests under
    // their versioned tag; a changed signature resets the whole entry (a
    // changed file invalidates its byte hash and every identity digest).

    pub fn cached_hash(&self, path_key: &str, signature: &[i64]) -> Option<String> {
        self.section("files", |files| {
            let entry = files.get(path_key)?.as_object()?;
            if entry.get("sig") == Some(&sig_value(signature)) {
                entry.get("sha256")?.as_str().map(str::to_string)
            } else {
                None
            }
        })
    }

    pub fn store_hash(&self, path_key: &str, signature: &[i64], digest: &str) {
        self.section_mut("files", |files| {
            let entry = entry_for(files, path_key, signature);
            entry.insert("sha256".to_string(), Value::String(digest.to_string()));
        });
    }

    /// A cached content-identity digest under its versioned tag (e.g.
    /// `jvm-abi@1`) — same stat validation as the byte hash, so an unchanged
    /// file costs one stat whether it is byte- or identity-hashed.
    pub fn cached_identity(&self, path_key: &str, signature: &[i64], tag: &str) -> Option<String> {
        self.section("files", |files| {
            let entry = files.get(path_key)?.as_object()?;
            if entry.get("sig") == Some(&sig_value(signature)) {
                entry.get(tag)?.as_str().map(str::to_string)
            } else {
                None
            }
        })
    }

    pub fn store_identity(&self, path_key: &str, signature: &[i64], tag: &str, digest: &str) {
        self.section_mut("files", |files| {
            let entry = entry_for(files, path_key, signature);
            entry.insert(tag.to_string(), Value::String(digest.to_string()));
        });
    }

    // -- action records --------------------------------------------------------

    pub fn action_record(&self, key: &str) -> Option<Value> {
        self.section("actions", |actions| actions.get(key).cloned())
    }

    pub fn record_action(&self, key: &str, record: Value) {
        self.section_mut("actions", |actions| {
            actions.insert(key.to_string(), record);
        });
    }

    /// Idempotent; also drops the action's discovered deps (mirrors Python).
    pub fn forget_action(&self, key: &str) {
        self.section_mut("actions", |actions| {
            actions.remove(key);
        });
        self.section_mut("deps", |deps| {
            deps.remove(key);
        });
    }

    // -- discovered (scanned) dependencies -------------------------------------

    pub fn discovered_deps(&self, key: &str) -> Option<Vec<String>> {
        self.section("deps", |deps| {
            deps.get(key)?.as_array().map(|list| {
                list.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()
            })
        })
    }

    pub fn set_discovered_deps(&self, key: &str, mut paths: Vec<String>) {
        paths.sort();
        self.section_mut("deps", |deps| {
            deps.insert(key.to_string(), json!(paths));
        });
    }

    // -- collection membership --------------------------------------------------

    pub fn membership(&self, key: &str) -> Option<Vec<String>> {
        self.section("memberships", |m| {
            m.get(key)?.as_array().map(|list| {
                list.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()
            })
        })
    }

    pub fn set_membership(&self, key: &str, mut members: Vec<String>) {
        members.sort();
        self.section_mut("memberships", |m| {
            m.insert(key.to_string(), json!(members));
        });
    }
}

fn sig_value(signature: &[i64]) -> Value {
    Value::Array(signature.iter().map(|&n| json!(n)).collect())
}

/// The sig-keyed entry for a path, reset if the signature changed
/// (mirrors `_entry_for`).
fn entry_for<'a>(
    files: &'a mut Map<String, Value>,
    path_key: &str,
    signature: &[i64],
) -> &'a mut Map<String, Value> {
    let sig = sig_value(signature);
    let needs_reset = match files.get(path_key) {
        Some(Value::Object(entry)) => entry.get("sig") != Some(&sig),
        _ => true,
    };
    if needs_reset {
        files.insert(path_key.to_string(), json!({ "sig": sig }));
    }
    match files.get_mut(path_key) {
        Some(Value::Object(entry)) => entry,
        _ => unreachable!(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "brix-metadata-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn roundtrip_and_lenient_load() {
        let root = scratch();
        let md = BuildMetadata::new(&root);
        md.record_action("k1", json!({"ok": true, "fingerprint": "f1"}));
        md.set_membership("m1", vec!["b".into(), "a".into()]);
        md.set_discovered_deps("k1", vec!["z.h".into(), "a.h".into()]);
        md.store_hash("/x", &[1, 2], "abc");
        md.save().unwrap();

        let again = BuildMetadata::new(&root);
        again.load();
        assert_eq!(again.action_record("k1").unwrap()["fingerprint"], "f1");
        assert_eq!(again.membership("m1").unwrap(), vec!["a", "b"]); // sorted
        assert_eq!(again.discovered_deps("k1").unwrap(), vec!["a.h", "z.h"]);
        assert_eq!(again.cached_hash("/x", &[1, 2]).unwrap(), "abc");
        assert_eq!(again.cached_hash("/x", &[1, 3]), None); // sig mismatch

        // Garbage and wrong versions reset to fresh, never crash.
        std::fs::write(&again.path, "not json").unwrap();
        again.load();
        assert!(again.action_record("k1").is_none());
        std::fs::write(&again.path, r#"{"version": 99}"#).unwrap();
        again.load();
        assert!(again.action_record("k1").is_none());
    }

    #[test]
    fn reads_python_written_shape_and_preserves_unknown_keys() {
        // A hand-written fixture in exactly the shape Python's json.dumps
        // produces, including an identity digest tag Rust does not compute.
        let root = scratch();
        let md = BuildMetadata::new(&root);
        std::fs::create_dir_all(&md.directory).unwrap();
        std::fs::write(
            &md.path,
            r#"{
 "actions": {
  "k": {
   "fingerprint": "fp",
   "ok": true,
   "outputs": {"build/x.txt": "h"},
   "result": {"action_name": "a", "argv": [], "command": "c", "exit_code": 0,
              "signal": null, "source": "process", "stderr": "", "stdout": "out"}
  }
 },
 "deps": {"k": ["src/a.h"]},
 "files": {"/abs/a.jar": {"jvm-abi@1": "idig", "sha256": "raw", "sig": [123, 4]}},
 "memberships": {"mk": ["a", "b"]},
 "version": 1
}"#,
        )
        .unwrap();
        md.load();
        let record = md.action_record("k").unwrap();
        assert_eq!(record["result"]["stdout"], "out");
        assert_eq!(md.membership("mk").unwrap(), vec!["a", "b"]);
        assert_eq!(md.discovered_deps("k").unwrap(), vec!["src/a.h"]);
        assert_eq!(md.cached_hash("/abs/a.jar", &[123, 4]).unwrap(), "raw");
        // Touch the byte hash; the Python-written identity digest must survive.
        md.store_hash("/abs/a.jar", &[123, 4], "raw2");
        md.save().unwrap();
        let text = std::fs::read_to_string(&md.path).unwrap();
        assert!(text.contains("jvm-abi@1"), "identity digest dropped:\n{text}");
        // A changed signature resets the entry, identity digests included.
        md.store_hash("/abs/a.jar", &[999, 5], "raw3");
        md.save().unwrap();
        let text = std::fs::read_to_string(&md.path).unwrap();
        assert!(!text.contains("jvm-abi@1"), "stale identity digest kept:\n{text}");
    }
}
