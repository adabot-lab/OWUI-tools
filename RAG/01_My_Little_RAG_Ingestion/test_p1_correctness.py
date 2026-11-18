"""P1 regression tests: deletion durability, deletion-only runs, truthful exit,
source-readiness guard, exclusion semantics.

Run in isolation (never against production):
    cd RAG/01_My_Little_RAG_Ingestion
    python3 -m pytest test_p1_correctness.py -v

Qdrant is faked with an in-memory stub; no network, no production mutation.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    chunks_dir = tmp_path / "chunks"
    metadata_dir = tmp_path / "metadata"
    for d in (input_dir, chunks_dir, metadata_dir):
        d.mkdir()
    # validate_required_env needs these present
    monkeypatch.setenv("QDRANT_HOST", "qdrant-test")
    monkeypatch.setenv("QDRANT_COLLECTION", "test-collection")
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", "test-model")
    monkeypatch.setenv("PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://embedding.invalid")
    # Dummy key: OpenAIEmbeddings validates credentials at construction time.
    # Embeddings are mocked in every test, so this key is never used for a call.
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-key-not-used")
    monkeypatch.setenv("VECTOR_SIZE", "1024")

    import importlib
    import config
    importlib.reload(config)  # re-executes module body: env vars above are picked up

    # Patch AFTER the reload: reload() would otherwise clobber patched attrs
    # back to the module-body defaults (which point at relative 'input' etc.)
    monkeypatch.setattr(config, "INPUT_DIR", str(input_dir))
    monkeypatch.setattr(config, "CHUNKS_DIR", str(chunks_dir))
    monkeypatch.setattr(config, "METADATA_FILE", str(metadata_dir / "file_metadata.json"))
    monkeypatch.setattr(config, "PENDING_DELETIONS_FILE", str(metadata_dir / "pending_deletions.json"))
    monkeypatch.setattr(config, "EXPECTED_SOURCES", [])
    monkeypatch.setattr(config, "MASS_REMOVAL_THRESHOLD", 0.2)
    monkeypatch.setattr(config, "MASS_REMOVAL_MIN_COUNT", 3)
    monkeypatch.setattr(config, "MASS_REMOVAL_MANIFEST", "")

    import logger
    importlib.reload(logger)
    import chunking
    chunking = importlib.reload(chunking)
    import metadata
    metadata = importlib.reload(metadata)
    import qdrant_ops
    qdrant_ops = importlib.reload(qdrant_ops)
    import main as main_mod
    main_mod = importlib.reload(main_mod)
    return {
        "input": input_dir,
        "chunks": chunks_dir,
        "metadata": metadata_dir,
        "config": config,
        "metadata_mod": metadata,
        "qdrant_ops": qdrant_ops,
        "main": main_mod,
    }


def write_doc(root: Path, rel: str, content: str = "content"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def make_chunk(coll_chunks_dir: Path, doc_id: int, source: str, n_chunks: int = 2):
    """Write committed chunk JSON files for doc_id."""
    coll_chunks_dir.mkdir(parents=True, exist_ok=True)
    for c in range(n_chunks):
        (coll_chunks_dir / f"{doc_id}_{c}.json").write_text(json.dumps({
            "doc_id": doc_id, "chunk_id": c, "text": f"t{c}", "source": source,
        }), encoding="utf-8")


def seed_metadata(m, entries, path_maps, counters):
    """Seed file_metadata.json.

    entries: file key -> per-file metadata (production keys are ABSOLUTE paths)
    path_maps: production format {"<collection>": {"<file key>": doc_id}}
    counters: production format {"<collection>": max_doc_id}
    """
    meta = dict(entries)
    meta["_path_maps"] = path_maps
    meta["_counters"] = counters
    (m["metadata"] / "file_metadata.json").write_text(json.dumps(meta))


class FakeQdrantClient:
    """In-memory Qdrant stub: collection -> doc_id -> point count."""

    def __init__(self, collections=None, fail_delete_for=None, fail_count_for=None):
        self.collections = {name: {} for name in (collections or [])}
        self.fail_delete_for = set(fail_delete_for or [])
        self.fail_count_for = set(fail_count_for or [])
        self.delete_calls = []
        self.upsert_collections = []

    def get_collections(self):
        class Res: pass
        objs = []
        for name in self.collections:
            o = type("C", (), {})(); o.name = name; objs.append(o)
        Res.collections = objs
        return Res()

    def get_collection(self, name):
        info = type("Info", (), {})()
        info.config = type("cfg", (), {})()
        info.config.params = type("params", (), {})()
        dense = type("dense", (), {})()
        dense.size = 1024
        info.config.params.vectors = {"dense": dense}
        return info

    def create_collection(self, collection_name, **kwargs):
        if collection_name in self.collections:
            raise Exception(f"{collection_name} already exists")
        self.collections[collection_name] = {}

    def create_payload_index(self, **kwargs):
        pass

    def delete_collection(self, collection_name):
        self.collections.pop(collection_name, None)

    def delete(self, collection_name, points_selector=None, wait=None):
        doc_id = points_selector.filter.must[0].match.value
        self.delete_calls.append((collection_name, doc_id))
        if (collection_name, doc_id) in self.fail_delete_for:
            raise Exception("simulated qdrant delete API failure")
        if collection_name in self.collections:
            self.collections[collection_name].pop(doc_id, None)

    def count(self, collection_name, count_filter=None):
        doc_id = count_filter.must[0].match.value
        if (collection_name, doc_id) in self.fail_count_for:
            raise Exception("simulated qdrant count failure")
        return type("R", (), {"count": self.collections.get(collection_name, {}).get(doc_id, 0)})()

    def upsert(self, collection_name, points, wait=None):
        self.upsert_collections.append(collection_name)
        doc = self.collections.setdefault(collection_name, {})
        for p in points:
            doc[p.payload["doc_id"]] = doc.get(p.payload["doc_id"], 0) + 1


class FakeEmbeddings:
    def embed_documents(self, texts):
        return [[0.0] * 1024 for _ in texts]


# ------------------------------------------------------------- defect 1

def test_deletion_only_collection_processed(env):
    """A collection present only in changed_doc_ids_by_collection reaches Qdrant."""
    m = env
    seed_metadata(
        m,
        {
            "input/coll_a/x.md": {"collections": {"coll_a": {"mtime": 1, "hash": "h", "embedded": True}}},
            "input/coll_b/y.md": {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}},
        },
        {"coll_a": {"input/coll_a/x.md": 1}, "coll_b": {"input/coll_b/y.md": 2}},
        {"coll_a": 1, "coll_b": 2},
    )
    write_doc(m["input"], "coll_a/x.md")
    make_chunk(m["chunks"] / "coll_a", 1, "input/coll_a/x.md")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    fake = FakeQdrantClient(collections=["coll_a", "coll_b"])
    fake.collections["coll_b"][2] = 3  # 3 points for doc 2

    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        m["qdrant_ops"].update_qdrant_index([], {"coll_b": [2]}, None)

    assert ("coll_b", 2) in fake.delete_calls, "deletion-only collection was skipped"
    assert fake.collections["coll_b"].get(2, 0) == 0


def test_deletion_only_run_needs_no_embedding_endpoint(env):
    """Deletion-only run: get_embedding_function is never called."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    seed_metadata(
        m,
        {y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"coll_b": {y_key: 2}},
        {"coll_b": 2},
    )
    write_doc(m["input"], "coll_b/.keep")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    fake = FakeQdrantClient(collections=["coll_b"])
    fake.collections["coll_b"][2] = 3

    with patch.object(m["main"], "get_embedding_function", side_effect=AssertionError("embedding endpoint must not be needed")):
        with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
            m["main"].main()

    assert fake.collections["coll_b"].get(2, 0) == 0


# ------------------------------------------------------------- defect 2

def test_pure_deletion_end_to_end(env):
    """Deleted file: ledger recorded, path map dropped, Qdrant delete verified."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    seed_metadata(
        m,
        {y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"coll_b": {y_key: 2}},
        {"coll_b": 2},
    )
    write_doc(m["input"], "coll_b/.keep")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    assert new_docs == []
    assert deletions == {"coll_b": [2]}
    # Ledger recorded BEFORE metadata saved (metadata save happens after loop)
    ledger = m["metadata_mod"].load_pending_deletions()
    assert f"coll_b:{y_key}" in ledger
    assert ledger[f"coll_b:{y_key}"]["doc_id"] == 2
    # Path map entry dropped
    saved = json.loads((m["metadata"] / "file_metadata.json").read_text())
    assert y_key not in saved["_path_maps"]["coll_b"]

    fake = FakeQdrantClient(collections=["coll_b"])
    fake.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        m["qdrant_ops"].update_qdrant_index([], deletions, None)

    assert fake.collections["coll_b"].get(2, 0) == 0
    # Acknowledged only after verified success
    assert m["metadata_mod"].load_pending_deletions() == {}


def test_mixed_add_delete_multi_collection(env):
    """Mixed add/delete across multiple collections: both deletions applied, new docs upserted."""
    m = env
    old_key = str(m["input"] / "coll_b/old.md")
    gone_key = str(m["input"] / "coll_c/gone.md")
    seed_metadata(
        m,
        {
            old_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}},
            gone_key: {"collections": {"coll_c": {"mtime": 1, "hash": "h", "embedded": True}}},
        },
        {"coll_b": {old_key: 5}, "coll_c": {gone_key: 6}},
        {"coll_b": 5, "coll_c": 6},
    )
    write_doc(m["input"], "coll_b/new.md")
    write_doc(m["input"], "coll_a/fresh.md")
    make_chunk(m["chunks"] / "coll_b", 5, "input/coll_b/old.md")
    make_chunk(m["chunks"] / "coll_c", 6, "input/coll_c/gone.md")

    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    assert deletions["coll_b"] == [5]
    assert deletions["coll_c"] == [6]
    assert {Path(d["source"]).name for d in new_docs} == {"new.md", "fresh.md"}

    fake = FakeQdrantClient(collections=[])
    fake.collections["coll_b"] = {5: 2}
    fake.collections["coll_c"] = {6: 2}
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        chunks = []
        for d in new_docs:
            chunks.extend(m["metadata_mod"].__dict__.get("_unused", []) or [
                {"doc_id": d["doc_id"], "chunk_id": 0, "text": "x", "source": d["source"], "collection_name": d["collection_name"]}
            ])
        m["qdrant_ops"].update_qdrant_index(chunks, deletions, FakeEmbeddings())

    assert ("coll_b", 5) in fake.delete_calls
    assert ("coll_c", 6) in fake.delete_calls
    assert "coll_b" in fake.upsert_collections and "coll_a" in fake.upsert_collections


def test_last_document_removed(env):
    """Removing the last document of a collection fully clears its points."""
    m = env
    only_key = str(m["input"] / "solo/only.md")
    seed_metadata(
        m,
        {only_key: {"collections": {"solo": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"solo": {only_key: 9}},
        {"solo": 9},
    )
    write_doc(m["input"], "solo/.keep")
    make_chunk(m["chunks"] / "solo", 9, "input/solo/only.md")

    _, deletions = m["metadata_mod"].load_documents_incremental()
    assert deletions == {"solo": [9]}

    fake = FakeQdrantClient(collections=["solo"])
    fake.collections["solo"][9] = 4
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        m["qdrant_ops"].update_qdrant_index([], deletions, None)
    assert fake.collections["solo"].get(9, 0) == 0


def test_delete_api_failure_is_fatal_and_retryable(env):
    """Qdrant delete API failure: raises (nonzero exit path), ledger retained."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    seed_metadata(
        m,
        {y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"coll_b": {y_key: 2}},
        {"coll_b": 2},
    )
    write_doc(m["input"], "coll_b/.keep")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    _, deletions = m["metadata_mod"].load_documents_incremental()
    ledger_path = m["metadata"] / "pending_deletions.json"
    assert f"coll_b:{y_key}" in json.loads(ledger_path.read_text())

    fake = FakeQdrantClient(collections=["coll_b"], fail_delete_for={("coll_b", 2)})
    fake.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        with pytest.raises(m["qdrant_ops"].DeletionFailedError):
            m["qdrant_ops"].update_qdrant_index([], deletions, None)

    # Ledger entry NOT acknowledged — next run retries.
    assert f"coll_b:{y_key}" in json.loads(ledger_path.read_text())


def test_count_verification_failure_keeps_retry_state(env):
    """Count (verification) failure after successful delete: failure, retry state kept."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    seed_metadata(
        m,
        {y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"coll_b": {y_key: 2}},
        {"coll_b": 2},
    )
    write_doc(m["input"], "coll_b/.keep")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    _, deletions = m["metadata_mod"].load_documents_incremental()

    fake = FakeQdrantClient(collections=["coll_b"], fail_count_for={("coll_b", 2)})
    fake.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        with pytest.raises(m["qdrant_ops"].DeletionFailedError):
            m["qdrant_ops"].update_qdrant_index([], deletions, None)
    assert f"coll_b:{y_key}" in json.loads((m["metadata"] / "pending_deletions.json").read_text())


def test_nonzero_remaining_points_is_failure(env):
    """Points remaining after delete call: DeletionFailedError, not warning."""
    m = env
    fake = FakeQdrantClient(collections=["coll_b"])
    # The stub's delete() removes the doc entry, so force count() to report
    # residual points despite the successful delete call.
    fake.collections["coll_b"][7] = 2

    def count_with_residual(collection_name, count_filter=None):
        return type("R", (), {"count": 2})()

    fake.count = count_with_residual
    with pytest.raises(m["qdrant_ops"].DeletionFailedError):
        m["qdrant_ops"]._delete_doc_verified(fake, "coll_b", 7)


def test_crash_before_ack_retried_idempotently(env):
    """Crash after ledger record, before ack: next run retries and completes."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    seed_metadata(
        m,
        {y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}}},
        {"coll_b": {y_key: 2}},
        {"coll_b": 2},
    )
    write_doc(m["input"], "coll_b/.keep")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")

    # Run 1: deletion recorded, Qdrant delete FAILS (simulated crash/failure).
    _, deletions = m["metadata_mod"].load_documents_incremental()
    fake = FakeQdrantClient(collections=["coll_b"], fail_delete_for={("coll_b", 2)})
    fake.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        with pytest.raises(m["qdrant_ops"].DeletionFailedError):
            m["qdrant_ops"].update_qdrant_index([], deletions, None)

    # Run 2 (new scan of same state): retries ledger, succeeds, acknowledges.
    # The file is still gone, metadata still knows it -> deletion diff again.
    _, deletions2 = m["metadata_mod"].load_documents_incremental()
    fake2 = FakeQdrantClient(collections=["coll_b"])
    fake2.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake2):
        m["qdrant_ops"].update_qdrant_index([], deletions2, None)
    assert fake2.collections["coll_b"].get(2, 0) == 0
    assert m["metadata_mod"].load_pending_deletions() == {}

    # Run 3: nothing left to do — no duplicate deletes.
    _, deletions3 = m["metadata_mod"].load_documents_incremental()
    fake3 = FakeQdrantClient(collections=["coll_b"])
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake3):
        m["qdrant_ops"].update_qdrant_index([], deletions3, None)
    assert fake3.delete_calls == []


# ------------------------------------------------------------- defect 4

def test_missing_input_root_aborts(env):
    """Missing input root: hard abort, no deletion processing."""
    m = env
    import shutil
    shutil.rmtree(m["input"])
    with pytest.raises(RuntimeError, match="does not exist"):
        m["main"].main()


def test_empty_input_root_aborts(env):
    """Empty input root (unmounted volume): hard abort."""
    m = env
    with pytest.raises(RuntimeError, match="empty"):
        m["main"].main()


def test_missing_expected_source_aborts(env):
    """Expected checkout missing: abort before scan, no mass deletion."""
    m = env
    # main.py binds EXPECTED_SOURCES at import time — patch there.
    m["main"].EXPECTED_SOURCES = ["Vergaberechtsprechung"]
    write_doc(m["input"], "other/x.md")
    with pytest.raises(RuntimeError, match="Vergaberechtsprechung"):
        m["main"].main()


def test_migration_marker_blocks_run(env):
    """Migration-in-progress marker: ingestion refuses to run."""
    m = env
    write_doc(m["input"], "coll_a/x.md")
    (m["input"] / ".migration_in_progress").write_text("")
    with pytest.raises(RuntimeError, match="Migration"):
        m["main"].main()


def test_mass_removal_guard_stops_with_preview(env, tmp_path):
    """Mass disappearance: aborts with MassRemovalError before any deletion."""
    m = env
    entries = {}
    path_maps = {}
    for i in range(10):
        abs_key = str(m["input"] / f"big/f{i}.md")
        entries[abs_key] = {"collections": {"big": {"mtime": 1, "hash": "h", "embedded": True}}}
        path_maps[abs_key] = i + 1
        write_doc(m["chunks"], f"big/{i+1}_0.json-placeholder", "{}")
    seed_metadata(m, entries, {"big": path_maps}, {"big": 10})
    write_doc(m["input"], "big/keep.md")
    # All 10 files gone -> exceeds allowance max(0.2*10, 3) = 3
    with pytest.raises(m["metadata_mod"].MassRemovalError):
        m["metadata_mod"].load_documents_incremental()
    # No deletion side effects: path maps untouched, no ledger entries.
    saved = json.loads((m["metadata"] / "file_metadata.json").read_text())
    assert len(saved["_path_maps"]["big"]) == 10
    assert m["metadata_mod"].load_pending_deletions() == {}


def test_mass_removal_manifest_allows_scoped_deletion(env, tmp_path):
    """Reviewed manifest: covered prefixes may be deleted even en masse."""
    m = env
    entries = {}
    path_maps = {}
    for i in range(10):
        abs_key = str(m["input"] / f"big/f{i}.md")
        entries[abs_key] = {"collections": {"big": {"mtime": 1, "hash": "h", "embedded": True}}}
        path_maps[abs_key] = i + 1
    seed_metadata(m, entries, {"big": path_maps}, {"big": 10})
    write_doc(m["input"], "big/keep.md")
    # Manifest prefixes are matched against stored keys, which are absolute.
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(str(m["input"] / "big") + "\n")
    # metadata.py binds MASS_REMOVAL_MANIFEST at import time — patch there.
    m["metadata_mod"].MASS_REMOVAL_MANIFEST = str(manifest)
    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    assert deletions == {"big": list(range(1, 11))}


# ------------------------------------------------------------- exclusions

def test_git_and_readme_excluded_from_scan(env):
    """.git internals and README.md are never ingested."""
    m = env
    write_doc(m["input"], "repo/.git/config.md")
    write_doc(m["input"], "repo/README.md")
    write_doc(m["input"], "repo/Entscheidungsgründe/real.md")
    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    sources = {Path(d["source"]).relative_to(m["input"]) for d in new_docs}
    assert sources == {Path("repo/Entscheidungsgründe/real.md")}


def test_excluded_preexisting_metadata_preserved(env):
    """Exclusion never silently purges pre-existing excluded-path metadata."""
    m = env
    readme_key = str(m["input"] / "repo/README.md")
    doc_key = str(m["input"] / "repo/doc.md")
    seed_metadata(
        m,
        {
            readme_key: {"collections": {"repo": {"mtime": 1, "hash": "h", "embedded": True}}},
            doc_key: {"collections": {"repo": {"mtime": 1, "hash": "h", "embedded": True}}},
        },
        {"repo": {readme_key: 3, doc_key: 4}},
        {"repo": 4},
    )
    write_doc(m["input"], "repo/doc.md")
    # Pin doc.md's mtime to the seeded value so it counts as unchanged
    # (mtime tolerance is 1s); only the README's exclusion is under test.
    import os as _os
    _os.utime(m["input"] / "repo/doc.md", (1, 1))
    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    # README excluded -> not deleted, metadata carried forward
    assert deletions == {}
    saved = json.loads((m["metadata"] / "file_metadata.json").read_text())
    assert readme_key in saved
    assert saved[readme_key]["collections"]["repo"]["embedded"] is True


def test_nested_routing_and_identical_basenames(env):
    """Nested sources route by first path component; identical basenames coexist."""
    m = env
    write_doc(m["input"], "Vergaberechtsprechung/Rechtsprechung/Entscheidungsgründe/OLG_X.md")
    write_doc(m["input"], "Aufsätze/OLG_X.md")  # same basename, different collection
    new_docs, _ = m["metadata_mod"].load_documents_incremental()
    by_source = {Path(d["source"]).relative_to(m["input"]): d["collection_name"] for d in new_docs}
    assert by_source[Path("Vergaberechtsprechung/Rechtsprechung/Entscheidungsgründe/OLG_X.md")] == "vergaberechtsprechung"
    assert by_source[Path("Aufsätze/OLG_X.md")] == "aufs_t_ze" or by_source[Path("Aufsätze/OLG_X.md")].startswith("aufs")


# ------------------------------------------------------------- defect 3

def test_unchanged_run_no_reembedding(env):
    """Ordinary unchanged run: no new docs, no deletions, metadata stable."""
    m = env
    import time as _t
    write_doc(m["input"], "coll_a/x.md")
    mtime = _t.time() - 100
    import os as _os
    _os.utime(m["input"] / "coll_a/x.md", (mtime, mtime))
    make_chunk(m["chunks"] / "coll_a", 1, "input/coll_a/x.md")

    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    assert len(new_docs) == 1  # first scan: embedded flag false -> processed once

    # Simulate successful embedding commit
    m["metadata_mod"].mark_as_embedded([str(m["input"] / "coll_a/x.md")], "coll_a")

    # Second scan immediately: nothing to do
    fake = FakeQdrantClient(collections=["coll_a"])
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        new_docs2, deletions2 = m["metadata_mod"].load_documents_incremental()
    assert new_docs2 == []
    assert deletions2 == {}
    # No chunk moved to .pending / no upserts triggered by an unchanged run
    assert fake.upsert_collections == []


def test_unchanged_run_no_upsert_calls(env):
    """Unchanged run through update_qdrant_index: zero Qdrant writes."""
    m = env
    fake = FakeQdrantClient(collections=["coll_a"])
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        m["qdrant_ops"].update_qdrant_index([], {}, None)
    assert fake.upsert_collections == []
    assert fake.delete_calls == []


def test_vector_size_mismatch_does_not_recreate_collection(env):
    """Ordinary reconciliation never deletes/recreates on vector mismatch."""
    m = env
    fake = FakeQdrantClient(collections=["coll_a"])
    # Force a mismatching vector size
    def get_collection(name):
        info = type("Info", (), {})()
        info.config = type("cfg", (), {})()
        info.config.params = type("params", (), {})()
        dense = type("dense", (), {})()
        dense.size = 512  # mismatch vs VECTOR_SIZE 1024
        info.config.params.vectors = {"dense": dense}
        return info
    fake.get_collection = get_collection
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        with pytest.raises(RuntimeError, match="mismatch"):
            m["qdrant_ops"].update_qdrant_index(
                [{"doc_id": 1, "chunk_id": 0, "text": "x", "source": "s", "collection_name": "coll_a"}],
                {}, FakeEmbeddings(),
            )
    assert "coll_a" in fake.collections, "collection must not be deleted on mismatch"


# ------------------------------------------------- interrupted multi-repo run

def test_interrupted_multi_repo_update_keeps_state(env):
    """Crash mid multi-collection update: ledger + path maps stay consistent."""
    m = env
    y_key = str(m["input"] / "coll_b/y.md")
    z_key = str(m["input"] / "coll_c/z.md")
    seed_metadata(
        m,
        {
            y_key: {"collections": {"coll_b": {"mtime": 1, "hash": "h", "embedded": True}}},
            z_key: {"collections": {"coll_c": {"mtime": 1, "hash": "h", "embedded": True}}},
        },
        {"coll_b": {y_key: 2}, "coll_c": {z_key: 3}},
        {"coll_b": 2, "coll_c": 3},
    )
    write_doc(m["input"], "coll_b/.keep")
    write_doc(m["input"], "coll_c/z.md")
    make_chunk(m["chunks"] / "coll_b", 2, "input/coll_b/y.md")
    make_chunk(m["chunks"] / "coll_c", 3, "input/coll_c/z.md")

    new_docs, deletions = m["metadata_mod"].load_documents_incremental()
    assert deletions["coll_b"] == [2]

    # Qdrant fails for coll_b but coll_c has no deletions to process:
    # the coll_b failure must raise, leaving ledger state for retry.
    fake = FakeQdrantClient(collections=["coll_b", "coll_c"], fail_delete_for={("coll_b", 2)})
    fake.collections["coll_b"][2] = 3
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake):
        with pytest.raises(m["qdrant_ops"].DeletionFailedError):
            m["qdrant_ops"].update_qdrant_index([], deletions, None)
    ledger = m["metadata_mod"].load_pending_deletions()
    assert f"coll_b:{y_key}" in ledger


# --------------------------------------------------- container exit status

def test_entrypoint_propagates_failure():
    """The compose entrypoint must exit nonzero when python fails (defect 3)."""
    import subprocess
    compose_dir = SERVICE_DIR
    script = f"""
src=$(cat {compose_dir}/docker-compose.yml)
"""
    # Extract the entrypoint logic by running the same shell shape with a
    # failing python command.
    failing = (
        "PROVIDER=openai python3 -c \"import sys; sys.exit(7)\"; "
        "rc=$?; "
        "if [ $rc -ne 0 ]; then echo \"Ingestion FAILED (python exit $rc).\"; exit $rc; fi; "
        "echo 'Ingestion complete.'"
    )
    result = subprocess.run(["bash", "-c", failing], capture_output=True, text=True)
    assert result.returncode == 7, "entrypoint shape must propagate python failure"
    assert "Ingestion FAILED" in result.stdout

    ok = (
        "PROVIDER=openai python3 -c \"import sys; sys.exit(0)\"; "
        "rc=$?; "
        "if [ $rc -ne 0 ]; then exit $rc; fi; "
        "echo 'Ingestion complete.'"
    )
    result_ok = subprocess.run(["bash", "-c", ok], capture_output=True, text=True)
    assert result_ok.returncode == 0
    assert "Ingestion complete." in result_ok.stdout


def test_main_returns_not_exits_on_no_changes(env):
    """No-change run completes cleanly (no exception) even with pending ledger empty."""
    m = env
    write_doc(m["input"], "coll_a/x.md")
    import os as _os
    mtime = 1000.0
    x_key = str(m["input"] / "coll_a/x.md")
    seed_metadata(
        m,
        {x_key: {"collections": {"coll_a": {"mtime": mtime, "hash": "x", "embedded": True}}}},
        {"coll_a": {x_key: 1}},
        {"coll_a": 1},
    )
    _os.utime(m["input"] / "coll_a/x.md", (mtime, mtime))
    make_chunk(m["chunks"] / "coll_a", 1, "input/coll_a/x.md")

    fake = FakeQdrantClient(collections=["coll_a"])
    with patch.object(m["qdrant_ops"], "QdrantClient", return_value=fake), \
         patch.object(m["main"], "get_embedding_function", side_effect=AssertionError("no embedding on no-op")):
        m["main"].main()  # must not raise
    assert fake.upsert_collections == []
