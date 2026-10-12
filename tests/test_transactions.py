import hashlib
import json
from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
import pytest
from test_core import graph
from test_hardening import assessment, node_edit
from bpt_process_agent.schemas import (Assumption, Checkpoint, Evidence, ExecutionState,
                                      Operation, Question, Resolution, UserConfirmation)
from bpt_process_agent.storage import (CorruptState, GraphStore, MutationRejected,
                                      StoreBusy, VersionConflict)
from bpt_process_agent.tracing import canonical_json

@pytest.fixture
def store(tmp_path, graph):
    store = GraphStore(tmp_path / "state.json")
    store.initialize(graph)
    return store

def resolution(status="confirmed", **kwargs):
    value = {"id": "r1", "status": status, "rationale": "Explicitly reviewed inference", "evidence_ids": ["e1"]}
    value.update(kwargs)
    return Operation("resolve_assumption", "assumptions", "a", value)

def infer(store, graph):
    node = asdict(graph.nodes["collect"])
    node.update(assumption_id="a", evidence_ids=[])
    return store.update_process_graph([
        Operation("add", "assumptions", "a", asdict(Assumption("a", "Original inference", ["e1"]))),
        Operation("update", "nodes", "collect", node),
    ], 0)

def test_agent_requires_assessment_and_records_target(store, graph):
    with pytest.raises(MutationRejected):
        store.update_process_graph([node_edit(graph)], 0, origin="agent", tool_call_id="missing-review")
    event = store.inspect_trace()[-1]
    assert event["tool_call_id"] == "missing-review"
    assert event["arguments"]["operations"][0]["id"] == "collect"
    assert event["outcomes"][0]["target_id"] == "collect"
    assert event["errors"]
    assert store.get_process_graph() == graph

def test_agent_assessed_transaction(store, graph):
    op = node_edit(graph, label="Collect quoted rates")
    result = store.update_process_graph([op], 0, origin="agent", assessments=[assessment(graph, op)], tool_call_id="agent-1")
    assert result.version == 1
    event = store.inspect_trace()[-1]
    assert event["outcomes"][0]["result"] == "committed"
    assert event["arguments"]["operations"][0]["value"]["label"] == "Collect quoted rates"
    assert event["arguments"]["assessments"][0]["scope_justification"]

@pytest.mark.parametrize("origin", ["manual", "agent"])
def test_explicit_out_of_scope_rejected(store, graph, origin):
    op = node_edit(graph)
    with pytest.raises(MutationRejected):
        store.update_process_graph([op], 0, origin=origin, assessments=[assessment(graph, op, status="out_of_scope")])
    assert store.get_process_graph() == graph

def test_assessment_cannot_cover_different_proposal(store, graph):
    op = node_edit(graph, label="Changed proposal")
    with pytest.raises(MutationRejected):
        store.update_process_graph([op], 0, origin="agent", assessments=[assessment(graph, node_edit(graph))])

def test_agent_batch_assesses_every_operation(store, graph):
    first = node_edit(graph)
    second = Operation("remove", "flows", "f3")
    with pytest.raises(MutationRejected):
        store.update_process_graph([first, second], 0, origin="agent", assessments=[assessment(graph, first)])
    assert store.get_process_graph() == graph
    outcomes = store.inspect_trace()[-2]["outcomes"]
    assert [v["result"] for v in outcomes] == ["rolled_back", "rejected"]

@pytest.mark.parametrize("action", ["update", "remove"])
def test_generic_assumption_change_rejected(store, graph, action):
    current = infer(store, graph)
    value = asdict(current.assumptions["a"]) if action == "update" else None
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation(action, "assumptions", "a", value)], 1)

@pytest.mark.parametrize("batch_kind", ["unlink", "delete_readd", "reassign", "resolve_then_unlink"])
def test_batched_lineage_bypasses_rejected(store, graph, batch_kind):
    current = infer(store, graph)
    fact = asdict(current.nodes["collect"])
    fact.update(assumption_id=None, evidence_ids=["e1"])
    if batch_kind == "unlink":
        ops = [Operation("update", "nodes", "collect", fact), Operation("remove", "assumptions", "a")]
    elif batch_kind == "delete_readd":
        ops = [Operation("remove", "nodes", "collect"), Operation("add", "nodes", "collect", fact)]
    elif batch_kind == "reassign":
        fact["assumption_id"] = "b"
        ops = [Operation("add", "assumptions", "b", asdict(Assumption("b", "Other inference", ["e1"]))),
               Operation("update", "nodes", "collect", fact)]
    else:
        ops = [resolution(), Operation("update", "nodes", "collect", fact)]
    with pytest.raises(MutationRejected):
        store.update_process_graph(ops, 1)
    assert store.get_process_graph() == current

def test_lineage_survives_deletion_across_versions(store, graph):
    current = infer(store, graph)
    node = asdict(current.nodes["collect"])
    store.update_process_graph([
        Operation("remove", "nodes", "collect"), Operation("remove", "flows", "f0"),
        Operation("remove", "flows", "f1")], 1)
    node.update(assumption_id=None, evidence_ids=["e1"])
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("add", "nodes", "collect", node)], 2)

def test_resolution_revised_then_confirmed_retains_original(store, graph):
    infer(store, graph)
    revised = store.update_process_graph([resolution("revised")], 1)
    confirmed = store.update_process_graph([resolution("confirmed", id="r2")], 2)
    a = confirmed.assumptions["a"]
    assert a.status == "confirmed"
    assert a.rationale == "Original inference"
    assert a.evidence_ids == ["e1"]
    assert [r.previous_status for r in a.history] == ["unverified", "revised"]
    assert confirmed.nodes["collect"].assumption_id == "a"
    assert GraphStore(store.path).get_process_graph() == confirmed
    with pytest.raises(MutationRejected):
        store.update_process_graph([resolution("confirmed", id="r3")], 3)
    reopened = store.update_process_graph([resolution("revised", id="r4")], 3)
    assert reopened.assumptions["a"].history[-1].previous_status == "confirmed"
    assert reopened.assumptions["a"].rationale == "Original inference"

@pytest.mark.parametrize("changes", [
    {"evidence_ids": []}, {"evidence_ids": ["missing"]},
    {"evidence_ids": [], "confirmation_id": "missing"}, {"rationale": ""},
])
def test_resolution_requires_real_support(store, graph, changes):
    current = infer(store, graph)
    with pytest.raises(MutationRejected):
        store.update_process_graph([resolution(**changes)], 1)
    assert store.get_process_graph() == current

def test_resolution_with_recorded_user_confirmation(store, graph):
    infer(store, graph)
    confirmation = UserConfirmation("c", "User explicitly confirms the inferred collection step", "test user", "2026-10-11T10:00:00+05:30")
    result = store.update_process_graph([
        Operation("add", "confirmations", "c", asdict(confirmation)),
        resolution(evidence_ids=[], confirmation_id="c")], 1)
    assert result.assumptions["a"].history[0].confirmation_id == "c"

def test_agent_cannot_fabricate_confirmation(store, graph):
    c = UserConfirmation("c", "Claimed confirmation", "agent", "2026-10-11T10:00:00+05:30")
    op = Operation("add", "confirmations", "c", asdict(c))
    with pytest.raises(MutationRejected):
        store.update_process_graph([op], 0, origin="agent", assessments=[assessment(graph, op)])

def test_rejection_requires_removing_active_inferred_element(store, graph):
    current = infer(store, graph)
    with pytest.raises(MutationRejected):
        store.update_process_graph([resolution("rejected")], 1)
    assert store.get_process_graph() == current
    result = store.update_process_graph([
        resolution("rejected"), Operation("remove", "nodes", "collect"),
        Operation("remove", "flows", "f0"), Operation("remove", "flows", "f1")], 1)
    assert result.assumptions["a"].status == "rejected"
    assert result.assumptions["a"].history

def test_boundary_lifecycle_and_stale_assessment(store, graph):
    old = assessment(graph, node_edit(graph))
    revised = store.update_process_graph([Operation("revise_boundary", "boundary", "boundary",
        {"end_condition": "", "end_resolved": False, "rationale": "End uncertain", "evidence_ids": ["e1"]})], 0)
    assert revised.boundary.revision == 1
    assert revised.boundary.history[0].previous_end == graph.boundary.end_condition
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("confirm_boundary", "boundary", "boundary",
            {"rationale": "Confirm", "evidence_ids": ["e1"]})], 1)
    with pytest.raises(MutationRejected):
        store.update_process_graph([node_edit(revised)], 1, origin="agent", assessments=[old])
    revised = store.update_process_graph([Operation("revise_boundary", "boundary", "boundary",
        {"end_condition": "Quotation delivered", "end_resolved": True, "rationale": "Clarified end", "evidence_ids": ["e1"]})], 1)
    confirmed = store.update_process_graph([Operation("confirm_boundary", "boundary", "boundary",
        {"rationale": "Reviewed trigger and end", "evidence_ids": ["e1"]})], 2)
    assert confirmed.boundary.status == "confirmed"
    assert len(confirmed.boundary.history) == 3
    assert not confirmed.complete

def test_boundary_revision_batch_cannot_reuse_old_review(store, graph):
    change = Operation("revise_boundary", "boundary", "boundary",
        {"end_condition": "Later end", "rationale": "Revised", "evidence_ids": ["e1"]})
    node = node_edit(graph)
    with pytest.raises(MutationRejected):
        store.update_process_graph([node, change], 0, origin="agent",
            assessments=[assessment(graph, node), assessment(graph, change)])

def test_version_checked_completion(store):
    store.update_process_graph([Operation("confirm_boundary", "boundary", "boundary",
        {"rationale": "Reviewed boundary", "evidence_ids": ["e1"]})], 0)
    result = store.update_process_graph([Operation("set_graph_state", "graph", "quotation",
        {"complete": True, "unresolved_issues": []})], 1)
    assert result.complete
    with pytest.raises(VersionConflict):
        store.update_process_graph([Operation("set_graph_state", "graph", "quotation", {"complete": False})], 1)

def test_execution_state_separate_versioned_and_reopenable(store):
    state = ExecutionState("run", graph_version=0, status="paused", budgets={"tool_calls_remaining": 10},
                           checkpoints=[Checkpoint("cp", 0, "Ready to resume")])
    store.save_execution_state(state, -1, 0)
    assert "execution_state" not in store.get_process_graph().to_dict()
    assert GraphStore(store.path).get_execution_state() == state
    with pytest.raises(VersionConflict):
        store.save_execution_state(state, -1, 0)
    state.version = 1
    state.status = "running"
    assert store.save_execution_state(state, 0, 0).version == 1

@pytest.mark.parametrize("change", [
    {"budgets": {"calls": -1}}, {"graph_version": 100}, {"run_id": ""},
    {"checkpoints": [Checkpoint("future", 4)]}, {"version": 4},
])
def test_invalid_execution_state(store, change):
    state = ExecutionState("run")
    for key, value in change.items():
        setattr(state, key, value)
    with pytest.raises(MutationRejected):
        store.save_execution_state(state, -1, 0)

def test_question_boundary_and_persistence(store, graph):
    store.save_execution_state(ExecutionState("run"), -1, 0)
    q = Question("q", "Who collects quotation rates?", "quotation")
    store.propose_question(q, assessment(graph, q), 0, tool_call_id="question-1")
    state = store.get_execution_state()
    assert state.pending_questions[0].question == q
    assert state.version == 1
    assert store.get_process_graph().version == 0
    with pytest.raises(MutationRejected):
        store.propose_question(q, assessment(graph, q), 0)

def test_question_out_of_scope_rejected(store, graph):
    store.save_execution_state(ExecutionState("run"), -1, 0)
    q = Question("q", "How are customers acquired?", "quotation")
    with pytest.raises(MutationRejected):
        store.propose_question(q, assessment(graph, q, status="out_of_scope"), 0)
    assert store.get_execution_state().pending_questions == []

def test_unresolved_endpoint_question_through_store(store):
    graph = store.update_process_graph([Operation("revise_boundary", "boundary", "boundary",
        {"start_resolved": False, "rationale": "Start uncertain", "evidence_ids": ["e1"]})], 0)
    store.save_execution_state(ExecutionState("run", graph_version=1), -1, 1)
    q = Question("q", "What starts quotation preparation?", "quotation", "start_boundary")
    store.propose_question(q, assessment(graph, q), 1)
    wrong = Question("q2", "What ends quotation preparation?", "quotation", "end_boundary")
    with pytest.raises(MutationRejected):
        store.propose_question(wrong, assessment(graph, wrong), 1)

def test_execution_cannot_bypass_question_assessment(store, graph):
    from bpt_process_agent.schemas import PendingQuestion
    q = Question("q", "How are customers acquired?", "quotation")
    state = ExecutionState("run", pending_questions=[PendingQuestion(q, assessment(graph, q, status="out_of_scope"), 0)])
    with pytest.raises(MutationRejected):
        store.save_execution_state(state, -1, 0)

def test_execution_must_refresh_after_graph_changes(store, graph):
    store.save_execution_state(ExecutionState("run"), -1, 0)
    updated = store.update_process_graph([node_edit(graph)], 0)
    q = Question("q", "Who collects rates?", "quotation")
    with pytest.raises(MutationRejected):
        store.propose_question(q, assessment(updated, q), 1)
    state = store.get_execution_state()
    state.graph_version = 1
    state.version += 1
    store.save_execution_state(state, 0, 1)
    store.propose_question(q, assessment(updated, q), 1)

def test_trace_contains_replayable_operations_and_errors(store, graph):
    op = node_edit(graph, label="Proposed changed label")
    with pytest.raises(MutationRejected):
        store.update_process_graph([op, Operation("remove", "nodes", "missing")], 0, tool_call_id="proposal-1")
    event = store.inspect_trace()[-1]
    saved = event["arguments"]["operations"][0]
    replay = Operation(**{key: value for key, value in saved.items() if key != "payload_sha256"})
    assert replay == op
    assert event["outcomes"][0]["result"] == "rolled_back"
    assert event["outcomes"][1]["error"]
    assert event["errors"]
    assert event["graph_version"] == 0

def test_duplicate_call_id_never_reapplies(store, graph):
    store.update_process_graph([node_edit(graph)], 0, tool_call_id="same")
    with pytest.raises(MutationRejected):
        store.update_process_graph([node_edit(graph)], 1, tool_call_id="same")
    assert store.get_process_graph().version == 1

@pytest.mark.parametrize("payload", [
    {"api_key": "private-test-value"},
    {"OPENAI_API_KEY": "private-test-value"},
    {"client_password": "private-test-value"},
    {"settings": {"password": "private-test-value"}},
    {"label": "Bearer private-test-value"},
    {"label": "Basic private-test-value"},
    {"label": "OPENAI_API_KEY=private-test-value"},
    {"label": "api_key=private-test-value"},
    {"label": "sk-private-test-value"},
])
def test_secrets_not_persisted_anywhere(store, graph, payload):
    op = node_edit(graph)
    value = dict(op.value)
    value.update(payload)
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("update", "nodes", "collect", value)], 0)
    for path in store.path.parent.iterdir():
        if path.is_file():
            assert "private-test-value" not in path.read_text(encoding="utf-8")
    assert "[REDACTED]" in json.dumps(store.inspect_trace())

def test_initial_secret_evidence_rejected_and_redacted(tmp_path, graph):
    e = graph.evidence["e1"]
    e.source_text = e.quote = "api_key=private-test-value"
    e.start, e.end = 0, len(e.quote)
    store = GraphStore(tmp_path / "state.json")
    with pytest.raises(MutationRejected):
        store.initialize(graph)
    assert "private-test-value" not in store.path.read_text()
    assert json.loads(store.path.read_text())["graph"] is None

def rewrite_with_checksum(path, transform):
    envelope = json.loads(path.read_text())
    transform(envelope)
    envelope.pop("checksum", None)
    envelope["checksum"] = hashlib.sha256(canonical_json(envelope).encode()).hexdigest()
    path.write_text(json.dumps(envelope))

@pytest.mark.parametrize("corruption", [
    "invalid_json", "checksum", "graph_mismatch", "bad_version", "invalid_history",
    "invalid_trace", "unknown_format", "invalid_execution",
])
def test_loading_validates_state(store, corruption):
    if corruption == "invalid_json":
        store.path.write_text("{truncated")
    elif corruption == "checksum":
        value = json.loads(store.path.read_text())
        value["graph"]["nodes"]["collect"]["label"] = "Tampered"
        store.path.write_text(json.dumps(value))
    else:
        def change(e):
            if corruption == "graph_mismatch":
                e["graph"]["nodes"]["collect"]["label"] = "Mismatch"
            elif corruption == "bad_version":
                e["versions"][0]["version"] = -1
            elif corruption == "invalid_history":
                e["versions"][0]["flows"]["f0"]["target_id"] = "missing"
            elif corruption == "invalid_trace":
                e["trace"][0]["sequence"] = 7
            elif corruption == "unknown_format":
                e["format_version"] = 99
            elif corruption == "invalid_execution":
                e["execution_state"] = asdict(ExecutionState("run", graph_version=99))
        rewrite_with_checksum(store.path, change)
    with pytest.raises(CorruptState):
        store.get_process_graph(tool_call_id="bad-load")
    failures = [json.loads(line) for line in store.failure_path.read_text().splitlines()]
    assert failures[-1]["tool_call_id"] == "bad-load"

def test_recover_corrupt_primary_from_independent_snapshot(store, graph):
    store.update_process_graph([node_edit(graph)], 0)
    snapshot_version = json.loads(store.snapshot_path.read_text())["graph"]["version"]
    store.path.write_text("{truncated")
    recovered = GraphStore(store.path).recover_from_snapshot(tool_call_id="recover-1")
    assert recovered.version == snapshot_version
    assert recovered == graph
    assert store.inspect_trace()[-1]["tool_call_id"] == "recover-1"
    assert store.get_process_graph() == recovered

def test_recovery_pauses_execution(store):
    state = ExecutionState("run", status="running")
    store.save_execution_state(state, -1, 0)
    store.get_process_graph()  # Snapshot now includes the saved state.
    store.path.write_text("{broken")
    store.recover_from_snapshot()
    assert store.get_execution_state().status == "paused"
    assert store.get_execution_state().version == 1

def test_recovery_never_steals_lock(store):
    store.lock_path.write_text("existing writer")
    with pytest.raises(StoreBusy):
        store.recover_from_snapshot()
    assert store.lock_path.exists()
    assert "StoreBusy" in store.failure_path.read_text()

def test_corrupt_snapshot_is_not_restored(store):
    before = store.path.read_bytes()
    store.snapshot_path.write_text("{broken")
    with pytest.raises(CorruptState):
        store.recover_from_snapshot()
    assert store.path.read_bytes() == before

def test_interrupted_primary_commit_preserves_recoverable_snapshot(store, graph, monkeypatch):
    import bpt_process_agent.storage as module
    replace = module.os.replace
    before = store.path.read_bytes()
    def fail_primary(source, target):
        if Path(target) == store.path:
            raise OSError("Interrupted primary replacement")
        return replace(source, target)
    monkeypatch.setattr(module.os, "replace", fail_primary)
    with pytest.raises(OSError):
        store.update_process_graph([node_edit(graph)], 0, tool_call_id="interrupted")
    assert store.path.read_bytes() == before
    assert store.snapshot_path.exists()
    assert not store.lock_path.exists()
    assert not list(store.path.parent.glob("*.tmp"))
    monkeypatch.setattr(module.os, "replace", replace)
    assert store.recover_from_snapshot() == graph
    assert "interrupted" in store.failure_path.read_text()

def test_legacy_state_loads_and_upgrades(store):
    envelope = json.loads(store.path.read_text())
    envelope.pop("checksum")
    envelope.pop("execution_state")
    envelope["format_version"] = 1
    for event in envelope["trace"]:
        for key in ["tool_call_id", "errors", "outcomes"]:
            event.pop(key)
    store.path.write_text(json.dumps(envelope))
    assert store.get_process_graph().version == 0
    upgraded = json.loads(store.path.read_text())
    assert upgraded["format_version"] == 2
    assert upgraded["checksum"]
    assert upgraded["trace"][0]["tool_call_id"] == "legacy-1"

def test_source_id_cannot_identify_different_text(store):
    e = Evidence("e2", "fixture", "Different source text", 0, 9, "Different")
    with pytest.raises(MutationRejected):
        store.update_process_graph([Operation("add", "evidence", "e2", asdict(e))], 0)


def test_rejected_assumption_can_be_explicitly_reopened(store, graph):
    infer(store, graph)
    store.update_process_graph([resolution("rejected"), Operation("remove", "nodes", "collect"),
        Operation("remove", "flows", "f0"), Operation("remove", "flows", "f1")], 1)
    with pytest.raises(MutationRejected):
        store.update_process_graph([resolution("confirmed", id="bad")], 2)
    reopened = store.update_process_graph([resolution("revised", id="reopen")], 2)
    assert reopened.assumptions["a"].status == "revised"
    assert reopened.assumptions["a"].history[-1].previous_status == "rejected"

def test_confirmation_cannot_be_updated_or_deleted(store):
    c = UserConfirmation("c", "Explicit user answer", "test user", "2026-10-11T10:00:00+05:30")
    store.update_process_graph([Operation("add", "confirmations", "c", asdict(c))], 0)
    for op in [Operation("remove", "confirmations", "c"),
               Operation("update", "confirmations", "c", asdict(c))]:
        with pytest.raises(MutationRejected):
            store.update_process_graph([op], 1)

def test_version_conflict_has_operation_outcomes(store, graph):
    with pytest.raises(VersionConflict):
        store.update_process_graph([node_edit(graph)], 99, tool_call_id="stale")
    event = store.inspect_trace()[-1]
    assert event["outcomes"][0]["target_id"] == "collect"
    assert event["outcomes"][0]["result"] == "rejected"

@pytest.mark.parametrize("id", ["", 99, "sk-private-test-value"])
def test_invalid_call_id_is_rejected_and_traced(store, graph, id):
    with pytest.raises(MutationRejected):
        store.update_process_graph([node_edit(graph)], 0, tool_call_id=id)
    assert store.get_process_graph() == graph
    assert store.inspect_trace()[-2]["errors"][0]["code"] == "invalid_call_id"
    assert "private-test-value" not in store.path.read_text()

def test_initialization_retry_retains_rejection_trace(tmp_path, graph):
    store = GraphStore(tmp_path / "retry.json")
    invalid = deepcopy(graph)
    invalid.nodes["collect"].scope_id = "outside"
    with pytest.raises(MutationRejected):
        store.initialize(invalid)
    store.initialize(graph)
    assert [event["result"] for event in store.inspect_trace()] == ["rejected", "ok"]
    assert json.loads(store.snapshot_path.read_text())["graph"] == graph.to_dict()

@pytest.mark.parametrize("corruption", ["trace_finding", "trace_timezone", "boundary_transition", "unknown_key"])
def test_additional_integrity_checks(store, graph, corruption):
    if corruption == "boundary_transition":
        store.update_process_graph([node_edit(graph)], 0)
    def change(e):
        if corruption == "trace_finding":
            e["trace"][0]["findings"] = [{"code": 5}]
        elif corruption == "trace_timezone":
            e["trace"][0]["timestamp"] = "2026-10-11T10:00:00"
        elif corruption == "unknown_key":
            e["unexpected"] = "unsupported envelope field"
        else:
            e["versions"][-1]["boundary"]["end_condition"] = "Unrecorded change"
            e["graph"]["boundary"]["end_condition"] = "Unrecorded change"
    rewrite_with_checksum(store.path, change)
    with pytest.raises(CorruptState):
        store.get_process_graph()

def test_actual_milestone_one_schema_loads(store):
    envelope = json.loads(store.path.read_text())
    envelope.pop("checksum")
    envelope.pop("execution_state")
    envelope["format_version"] = 1
    for value in [envelope["graph"], *envelope["versions"]]:
        value.pop("confirmations")
        for key in ["start_resolved", "end_resolved", "revision", "history"]:
            value["boundary"].pop(key)
        for p in value["personas"].values():
            p.pop("organizational_context")
    for event in envelope["trace"]:
        for key in ["tool_call_id", "errors", "outcomes"]:
            event.pop(key)
    store.path.write_text(json.dumps(envelope))
    graph = store.get_process_graph()
    assert graph.boundary.revision == 0
    assert graph.personas["sales"].organizational_context == ""


def test_fixed_agent_adapter_cannot_select_manual_origin(store, graph):
    tools = store.agent_tools()
    op = node_edit(graph)
    with pytest.raises(MutationRejected):
        tools.update_process_graph([op], 0)
    with pytest.raises(TypeError):
        tools.update_process_graph([op], 0, origin="manual")
    assert not hasattr(tools, "initialize")
    assert not hasattr(tools, "recover_from_snapshot")
    assert not hasattr(tools, "save_execution_state")
    result = tools.update_process_graph([op], 0, assessments=[assessment(graph, op)])
    assert result.version == 1
    assert tools.validate_process_graph() == []
    assert tools.get_process_graph() == result
