from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import pytest
from bpt_process_agent.ingestion.repository import EvidenceStore, EvidenceStoreError
from bpt_process_agent.ingestion.parsers import parse_document
from bpt_process_agent.ingestion.models import CandidateFact, Citation, AuditFinding, stable_id, fact_id
from bpt_process_agent.ingestion.validation import EvidenceValidationError

def setup(store, path):
    parsed, data = parse_document(path)
    id = store.ingest(parsed, data)
    run = store.start_run([id], [path.name])
    block = next(b for b in parsed.blocks if b.text == "Sales collects vendor rates.")
    fact = CandidateFact(block.text, ["activity", "role"], "as_is",
                         [Citation(block.id, 0, len(block.text), block.text)], "")
    return parsed, run, fact

def test_immutable_source_and_repeat_dedup(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    ids = store.add_facts(run, [fact], {fact.citations[0].block_id})
    store.ingest(parsed, detailed_sop.read_bytes())
    store.add_facts(run, [fact], {fact.citations[0].block_id})
    state = store.load()
    assert len(state.documents) == 1 and len(state.facts) == 1
    assert len(state.facts[ids[0]].versions) == 1
    assert (store.directory / parsed.document.archive_name).read_bytes() == detailed_sop.read_bytes()
    assert store.search_sources("vendor rates")[0]["block"]["text"] == fact.statement

@pytest.mark.parametrize("target", ["source", "fact_history", "delete_fact"])
def test_attempts_to_bypass_immutability_are_atomic_and_traced(tmp_path, detailed_sop, target):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.add_facts(run, [fact], {fact.citations[0].block_id})[0]
    before = store.load()
    def invalid(state):
        if target == "source": state.blocks[fact.citations[0].block_id].text = "invented"
        if target == "fact_history": state.facts[id].versions[0].statement = "invented"
        if target == "delete_fact": del state.facts[id]
    with pytest.raises(EvidenceStoreError):
        store.commit(before.version, "invalid_batch", {"target_ids": [id]}, invalid)
    after = store.load()
    assert after.facts == before.facts and after.blocks == before.blocks
    assert after.trace[-1]["outcome"] == "rejected"
    assert after.trace[-1]["target_element_ids"] == [id]

def test_stale_version_is_traced_without_changing_evidence(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    setup(store, detailed_sop)
    before = store.load()
    with pytest.raises(EvidenceStoreError):
        store.commit(before.version - 1, "stale", {}, lambda s: s.facts.clear())
    assert store.load().facts == before.facts
    assert store.load().trace[-1]["outcome"] == "rejected"

def test_repeated_disagreement_preserves_original(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.add_facts(run, [fact], {fact.citations[0].block_id})[0]
    fact.statement = "Sales approves vendor rates."
    store.add_facts(run, [fact], {fact.citations[0].block_id})
    state = store.load()
    assert state.facts[id].versions[-1].statement == "Sales collects vendor rates."
    assert state.runs[run].findings[0].kind == "repeat_disagreement"

def test_correction_preserves_history_and_needs_verification(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.add_facts(run, [fact], {fact.citations[0].block_id})[0]
    finding = AuditFinding("audit-finding", "incorrect_classification", "Timing is not specified",
                           [id], fact.citations, "ambiguous", ["activity"], call_id="initial-audit")
    store.record_findings(run, [finding])
    store.revise_from_audit(run, finding.id)
    versions = store.load().facts[id].versions
    assert len(versions) == 2 and versions[0].classification == "as_is"
    assert versions[1].classification == "ambiguous" and versions[1].audit_finding_id == finding.id
    call = store.begin_call(run, "evidence_audit", "test/pro", {})
    store.finish_call(run, call, result={"value": {"resolved_finding_ids": [finding.id], "findings": []}})
    store.resolve_findings(run, [finding.id], call)
    assert store.load().runs[run].findings[0].status == "resolved"

def test_source_corruption_and_primary_recovery(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    store.add_facts(run, [fact], {fact.citations[0].block_id})
    snapshot = tmp_path / "separate-snapshot.json"
    snapshot.write_bytes(store.path.read_bytes())
    store.path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(EvidenceStoreError): store.load()
    store.recover(snapshot)
    assert len(store.load().facts) == 1
    (store.directory / parsed.document.archive_name).write_bytes(b"bad")
    with pytest.raises(EvidenceStoreError): store.load()
    with pytest.raises(EvidenceStoreError): store.recover(snapshot)

def test_interrupted_commit_leaves_valid_snapshot(tmp_path, detailed_sop, monkeypatch):
    store = EvidenceStore(tmp_path / "store")
    setup(store, detailed_sop)
    before = store.load()
    original = store._write
    def interrupted(path, state):
        if path == store.path: raise OSError("simulated interrupted replacement")
        original(path, state)
    monkeypatch.setattr(store, "_write", interrupted)
    with pytest.raises(OSError):
        store.commit(before.version, "interrupted", {}, lambda s: None)
    assert store.load().version == before.version
    assert store._read(store.backup).version == before.version

def test_trace_redacts_sensitive_arguments(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    store.commit(store.load().version, "bad_input", {"OPENROUTER_API_KEY": "secret-value"}, lambda s: None)
    assert "secret-value" not in store.path.read_text(encoding="utf-8")
    assert store.load().trace[-1]["arguments"]["OPENROUTER_API_KEY"] == "[REDACTED]"

def test_pending_call_remains_visible_after_interruption(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.begin_call(run, "extraction", "test/flash", {"source_blocks": [fact.citations[0].block_id]})
    loaded = EvidenceStore(store.directory).load()
    assert loaded.runs[run].events[-1]["status"] == "pending"
    assert id in loaded.runs[run].call_ids

def test_batched_invalid_candidate_accepts_nothing(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    invalid = deepcopy(fact)
    invalid.citations[0].quote = "invented"
    with pytest.raises(EvidenceStoreError):
        store.add_facts(run, [fact, invalid], {fact.citations[0].block_id})
    assert not store.load().facts
    assert store.load().trace[-1]["outcome"] == "rejected"

def test_loaded_bad_revision_rejected_even_with_recomputed_checksum(tmp_path, detailed_sop):
    from bpt_process_agent.tracing import canonical_json
    import hashlib
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    store.add_facts(run, [fact], {fact.citations[0].block_id})
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    item = next(iter(envelope["registry"]["facts"].values()))
    revised = deepcopy(item["versions"][0]); revised["revision"] = 2; revised["audit_finding_id"] = "invented"
    item["versions"].append(revised)
    envelope["sha256"] = hashlib.sha256(canonical_json(envelope["registry"]).encode()).hexdigest()
    store.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(EvidenceStoreError): store.load()

def test_source_change_cannot_bypass_checksum(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    setup(store, detailed_sop)
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    next(iter(envelope["registry"]["blocks"].values()))["text"] = "changed"
    store.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(EvidenceStoreError): store.load()

def test_live_writer_lock_blocks_mutation_and_recovery(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    setup(store, detailed_sop)
    store.lock.write_text("active", encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(EvidenceStoreError): store.commit(store.load().version, "locked", {}, lambda s: None)
    with pytest.raises(EvidenceStoreError): store.recover(store.backup)
    assert store.path.read_bytes() == before

def test_ambiguity_cannot_be_resolved_through_audit_api(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.add_facts(run, [fact], {fact.citations[0].block_id})[0]
    f = AuditFinding("ambiguity", "ambiguous", "Source ambiguity", [id], fact.citations, None, None)
    store.record_findings(run, [f])
    call = store.begin_call(run, "evidence_audit", "fixture/pro", {})
    store.finish_call(run, call, result={"value": {"findings": [], "resolved_finding_ids": [f.id]}})
    with pytest.raises(EvidenceStoreError): store.resolve_findings(run, [f.id], call)
    assert store.load().runs[run].findings[0].status == "open"

def test_omission_resolution_needs_recovered_fact(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    f = AuditFinding("missing", "missing", "Missing source fact", [], fact.citations, None, None, "recovery_attempted")
    store.record_findings(run, [f])
    call = store.begin_call(run, "evidence_audit", "fixture/pro", {})
    store.finish_call(run, call, result={"value": {"findings": [], "resolved_finding_ids": [f.id]}})
    with pytest.raises(EvidenceStoreError): store.resolve_findings(run, [f.id], call)

def test_resolution_needs_declared_verification_result(tmp_path, detailed_sop):
    store = EvidenceStore(tmp_path / "store")
    parsed, run, fact = setup(store, detailed_sop)
    id = store.add_facts(run, [fact], {fact.citations[0].block_id})[0]
    f = AuditFinding("unsupported", "unsupported", "Unsupported paraphrase", [id], [], None, None)
    store.record_findings(run, [f]); store.revise_from_audit(run, f.id)
    call = store.begin_call(run, "evidence_audit", "fixture/pro", {})
    store.finish_call(run, call, result={"value": {"findings": [], "resolved_finding_ids": []}})
    with pytest.raises(EvidenceStoreError): store.resolve_findings(run, [f.id], call)
