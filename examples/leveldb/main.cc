// Smoke test for a brix-built libleveldb.a: open a fresh database, write a
// few keys, read one back, iterate, and confirm the round-trip. Exits non-zero
// on any failure so `python brix.py smoke` doubles as a build acceptance test.
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "leveldb/db.h"
#include "leveldb/write_batch.h"

int main() {
  std::string dir = "/tmp/brix-leveldb-smoke";
  // Start from a clean slate so repeated runs are deterministic.
  std::string rm = "rm -rf '" + dir + "'";
  if (std::system(rm.c_str()) != 0) return 1;

  leveldb::DB* db = nullptr;
  leveldb::Options options;
  options.create_if_missing = true;
  leveldb::Status status = leveldb::DB::Open(options, dir, &db);
  if (!status.ok()) {
    std::fprintf(stderr, "open failed: %s\n", status.ToString().c_str());
    return 1;
  }

  leveldb::WriteBatch batch;
  batch.Put("apple", "red");
  batch.Put("banana", "yellow");
  batch.Put("grape", "purple");
  status = db->Write(leveldb::WriteOptions(), &batch);
  if (!status.ok()) {
    std::fprintf(stderr, "write failed: %s\n", status.ToString().c_str());
    return 1;
  }

  std::string value;
  status = db->Get(leveldb::ReadOptions(), "banana", &value);
  if (!status.ok() || value != "yellow") {
    std::fprintf(stderr, "get(banana) => %s (%s)\n", value.c_str(),
                 status.ToString().c_str());
    return 1;
  }

  int count = 0;
  leveldb::Iterator* it = db->NewIterator(leveldb::ReadOptions());
  for (it->SeekToFirst(); it->Valid(); it->Next()) ++count;
  if (!it->status().ok()) {
    std::fprintf(stderr, "iteration failed: %s\n", it->status().ToString().c_str());
    return 1;
  }
  delete it;
  delete db;

  if (count != 3) {
    std::fprintf(stderr, "expected 3 rows, saw %d\n", count);
    return 1;
  }

  std::printf("leveldb smoke ok: banana=%s, %d keys\n", value.c_str(), count);
  return 0;
}
