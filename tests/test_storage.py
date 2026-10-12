from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
import pytest
from bpt_process_agent.schemas import Operation
from bpt_process_agent.storage import GraphStore, MutationRejected, StoreBusy, StoreError, VersionConflict
from test_core import graph

@pytest.fixture
def store(tmp_path, graph):
    result = GraphStore(tmp_path / "state.json")
    result.initialize(graph)
    return result


def edit(graph, label="Collect rates manually"):
    node = deepcopy(graph.nodes["collect"])
    node.label = label
    return Operation("update", "nodes", node.id, asdict(node))


def test_persistence_version_history_and_trace(store, graph):
    updated = store.update_process_graph([edit(graph)], 0)
    assert updated.version == 1
    reopened = GraphStore(store.path)
    assert reopened.get_process_graph() == updated
    assert reopened.validate_process_graph() == []
    envelope = json.loads(store.path.read_text())
    assert [g["version"] for g in envelope["versions"]] == [0, 1]
    assert [e["tool"] for e in envelope["trace"]] == ["initialize", "update_process_graph", "get_process_graph", "validate_process_graph"]
    assert envelope["trace"][1]["graph_version"] == 1
    assert envelope["trace"][1]["arguments"]["operations"][0]["payload_sha256"]


def test_failed_batch_is_atomic(store, graph):
    with pytest.raises(MutationRejected):
        store.update_process_graph([edit(graph), Operation("remove", "nodes", "end")], 0)
    assert store.get_process_graph() == graph
    event = store.inspect_trace()[-2]
    assert event["result"] == "rejected"
    assert event["graph_version"] == 0
    assert any(f["code"] == "flow_reference" for f in event["findings"])
    assert len(json.loads(store.path.read_text())["versions"]) == 1

@pytest.mark.parametrize("version", [-1, 1, True, "0"])
def test_version_conflict(store, graph, version):
    with pytest.raises(VersionConflict):
        store.update_process_graph([edit(graph)], version)
    assert store.get_process_graph() == graph
    assert store.inspect_trace()[-2]["result"] == "version_conflict"

@pytest.mark.parametrize("operations", [
    [], ["not an operation"],
    [Operation("add", [], "x")],
    [Operation("add", "nodes", [], {})],
    [Operation("invalid", "nodes", "collect")],
    [Operation("add", "nodes", "collect", {})],
    [Operation("remove", "nodes", "missing")],
    [Operation("update", "nodes", "collect", {"unexpected": True})],
    [Operation("update", "nodes", "collect", {"id": "collect"})],
    [Operation("remove", "nodes", "collect", {})],
    [Operation("update", "boundary", "boundary", {})],
])
def test_malformed_batches(store, graph, operations):
    with pytest.raises(MutationRejected):
        store.update_process_graph(operations, 0)
    assert store.get_process_graph() == graph
    assert store.inspect_trace()[-2]["result"] == "rejected"


def test_out_of_boundary_rejected(store, graph):
    node = asdict(graph.nodes["collect"])
    node["scope_id"] = "fulfillment"
    with pytest.raises(MutationRejected) as caught:
        store.update_process_graph([Operation("update", "nodes", "collect", node)], 0)
    assert "out_of_boundary" in {f.code for f in caught.value.findings}
    assert store.get_process_graph() == graph


def test_evidence_is_immutable(store, graph):
    evidence = asdict(graph.evidence["e1"])
    evidence["source_id"] = "other"
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("update", "evidence", "e1", evidence)], 0)


def test_all_references_can_change_in_one_batch(store, graph):
    # Intermediate dangling references are permissible within an atomic batch.
    store.update_process_graph([Operation("remove", "nodes", "end"), Operation("remove", "flows", "f3")], 0)
    assert "end" not in store.get_process_graph().nodes


def test_returned_graph_cannot_change_disk(store):
    graph = store.get_process_graph()
    graph.nodes.clear()
    assert store.get_process_graph().nodes


def test_failure_to_replace_preserves_entire_envelope(store, graph, monkeypatch):
    before = store.path.read_bytes()
    def fail(*args):
        raise OSError("Simulated disk failure")
    monkeypatch.setattr("bpt_process_agent.storage.os.replace", fail)
    with pytest.raises(OSError):
        store.update_process_graph([edit(graph)], 0)
    assert store.path.read_bytes() == before
    assert not store.lock_path.exists()
    assert not list(store.path.parent.glob("*.tmp"))


def test_initialize_never_overwrites(store, graph):
    before = json.loads(store.path.read_text())["graph"]
    with pytest.raises(StoreError):
        store.initialize(graph)
    assert json.loads(store.path.read_text())["graph"] == before
    assert store.inspect_trace()[-1]["findings"][0]["code"] == "already_initialized"


def test_invalid_initialization_is_traced(tmp_path, graph):
    graph.nodes["collect"].scope_id = "outside"
    store = GraphStore(tmp_path / "bad.json")
    with pytest.raises(MutationRejected):
        store.initialize(graph)
    assert store.inspect_trace()[0]["result"] == "rejected"
    assert json.loads(store.path.read_text())["graph"] is None


def test_lock_does_not_change_store(store, graph):
    before = store.path.read_bytes()
    store.lock_path.write_text("other writer")
    with pytest.raises(StoreBusy):
        store.update_process_graph([edit(graph)], 0)
    assert store.path.read_bytes() == before
    assert store.lock_path.exists()


def test_concurrent_writers_cannot_lose_update(store, graph):
    def writer(label):
        try:
            store.update_process_graph([edit(graph, label)], 0)
            return "ok"
        except (StoreBusy, VersionConflict):
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(writer, ["A", "B"]))
    assert sorted(results) == ["ok", "rejected"]
    assert store.get_process_graph().version == 1


def test_trace_does_not_echo_untrusted_secrets(store, graph):
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("update", "nodes", "collect", {"api_key": "sk-secret-value"})], 0)
    assert "sk-secret-value" not in json.dumps(store.inspect_trace())


def test_evidence_registry_is_append_only(store):
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("remove", "evidence", "e1")], 0)
