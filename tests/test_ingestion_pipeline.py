from dataclasses import asdict
import json
from pathlib import Path
import re
import pytest
from bpt_process_agent.ingestion.llm import ModelResult
from bpt_process_agent.ingestion.pipeline import ExtractionPipeline
from bpt_process_agent.ingestion.repository import EvidenceStore
from bpt_process_agent.ingestion.report import write_report
from bpt_process_agent.llm_config import LLMConfig

class ScriptedClient:
    """Deterministic fixture oracle. All responses and capability calls are in-process."""
    def __init__(self, mode="normal"):
        self.mode = mode
        self.calls = []
    def verify(self, model):
        self.calls.append(("verify", model))
        if self.mode == "unsupported": raise ValueError("unsupported model")
        return {"model": model, "providers": ["fixture"], "supported_parameters": ["structured_outputs", "response_format"]}
    def complete(self, model, messages, schema, *, name):
        data = json.loads(messages[1]["content"])
        self.calls.append((name, model, data))
        views = data["source_views"]
        if name == "process_evidence":
            facts = []
            for view in views:
                for match in re.finditer(r"[^\n]+", view["text"]):
                    text = match.group()
                    if not any(term in text for term in ["Sales ", "To-Be:", "quotation must", "Manual re-entry", "CRM"]):
                        continue
                    cite = {"block_id": view["id"], "start": view["start"] + match.start(),
                            "end": view["start"] + match.end(), "quote": text}
                    cls = "to_be" if "To-Be:" in text else "as_is"
                    tags = ["activity", "role"] if "Sales " in text else ["rule", "SLA"] if "two working days" in text else ["pain_point"] if "Manual" in text else ["system", "document"] if "CRM" in text else ["activity", "system"]
                    if "If a rate" in text: tags += ["decision", "exception"]
                    missing = data["recover_only_omission"]
                    if self.mode == "omission" and "sends the quote" in text and missing is None: continue
                    if self.mode == "misclassified" and "To-Be:" in text and missing is None: cls = "as_is"
                    statement = "Sales approves all quotations." if self.mode == "unsupported_fact" and "collects" in text else text
                    if missing and not any(c["block_id"] == cite["block_id"] and c["start"] < cite["end"] and cite["start"] < c["end"] for c in missing["citations"]):
                        continue
                    facts.append({"statement": statement, "tags": tags, "classification": cls, "citations": [cite], "ambiguity": ""})
            if self.mode == "invalid_quote" and facts: facts[0]["citations"][0]["quote"] = "invented"
            return ModelResult({"facts": facts}, "fixture-extraction", {"total_tokens": 10})
        facts, previous = data["facts"], data["previous_findings"]
        if previous:
            return ModelResult({"findings": [], "resolved_finding_ids": [f["id"] for f in previous]})
        findings = []
        def finding(kind, description, ids, cites, cls=None):
            return {"kind": kind, "description": description, "fact_ids": ids, "citations": cites,
                    "suggested_classification": cls, "suggested_tags": None}
        if self.mode == "omission":
            for v in views:
                match = re.search(r"Sales sends the quote to the customer\.", v["text"])
                if match and not any("sends the quote" in f["current"]["statement"] for f in facts):
                    cite = {"block_id": v["id"], "start": v["start"] + match.start(), "end": v["start"] + match.end(), "quote": match.group()}
                    findings.append(finding("missing", "Customer send step is missing", [], [cite]))
        if self.mode == "misclassified":
            for f in facts:
                if "To-Be:" in f["current"]["statement"] and f["current"]["classification"] == "as_is":
                    findings.append(finding("incorrect_classification", "Explicit future-state proposal", [f["id"]], f["current"]["citations"], "to_be"))
        if self.mode == "unsupported_fact":
            for f in facts:
                if "approves all" in f["current"]["statement"] and f["current"]["status"] == "active":
                    findings.append(finding("unsupported", "No approval rule is stated", [f["id"]], []))
        if self.mode == "conflicts" and len(facts) >= 2:
            findings.append(finding("conflict", "Markup policies disagree", [f["id"] for f in facts],
                                    [c for f in facts for c in f["current"]["citations"]]))
        if self.mode == "audit_error": raise RuntimeError("provider failure")
        return ModelResult({"findings": findings, "resolved_finding_ids": []}, "fixture-audit")

def execute(tmp_path, paths, mode="normal", **kwargs):
    store = EvidenceStore(tmp_path / "registry")
    client = ScriptedClient(mode)
    config = LLMConfig("test-credential", "fixture/flash", "fixture/pro")
    run = ExtractionPipeline(store, client, config, **kwargs).run(paths)
    return store, client, store.load().runs[run]

def test_sparse_slide_extracts_all_steps_multitags_and_to_be(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide])
    facts = [f.versions[-1] for f in store.load().facts.values()]
    assert len(facts) == 4
    assert sum(f.classification == "as_is" for f in facts) == 3
    assert sum(f.classification == "to_be" for f in facts) == 1
    assert all("role" in f.tags and "activity" in f.tags for f in facts if f.classification == "as_is")
    assert run.status == "audited" and run.coverage == "audited_parsed_content"
    assert [c[1] for c in client.calls if c[0] == "process_evidence"] == ["fixture/flash"]
    assert [c[1] for c in client.calls if c[0] == "evidence_audit"] == ["fixture/pro"]
    audit = next(c[2] for c in client.calls if c[0] == "evidence_audit")
    assert "source_views" in audit and len(audit["source_views"]) == 2

def test_sop_structured_content_and_sla(tmp_path, detailed_sop):
    store, client, run = execute(tmp_path, [detailed_sop])
    facts = [f.versions[-1] for f in store.load().facts.values()]
    assert any(f.tags == ["SLA", "rule"] for f in facts)
    assert any(f.tags == ["document", "system"] for f in facts)
    assert any("decision" in f.tags and "exception" in f.tags for f in facts)
    assert run.status == "audited"

def test_independent_audit_recovers_omission_with_exact_lineage(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide], "omission")
    finding = next(f for f in run.findings if f.kind == "missing")
    recovered = next(f for f in store.load().facts.values() if "sends the quote" in f.versions[0].statement)
    assert finding.status == "resolved"
    assert recovered.versions[0].audit_finding_id == finding.id
    assert recovered.versions[0].reason == "omission recovery"
    assert len([c for c in client.calls if c[0] == "process_evidence"]) == 2
    assert len([c for c in client.calls if c[0] == "evidence_audit"]) == 2
    assert run.status == "audited"

def test_audit_classification_correction_is_versioned(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide], "misclassified")
    fact = next(f for f in store.load().facts.values() if "To-Be:" in f.versions[0].statement)
    assert [v.classification for v in fact.versions] == ["as_is", "to_be"]
    assert fact.versions[1].audit_finding_id
    assert next(f for f in run.findings if f.kind == "incorrect_classification").status == "resolved"

def test_unsupported_fact_is_quarantined_not_deleted(tmp_path, detailed_sop):
    store, client, run = execute(tmp_path, [detailed_sop], "unsupported_fact")
    fact = next(f for f in store.load().facts.values() if "approves all" in f.versions[0].statement)
    assert [v.status for v in fact.versions] == ["active", "quarantined"]
    assert len(fact.versions) == 2
    assert run.status == "audited"

def test_conflicting_documents_remain_separate_unresolved(tmp_path, conflicting_documents):
    store, client, run = execute(tmp_path, conflicting_documents, "conflicts")
    facts = list(store.load().facts.values())
    assert len(facts) == 2 and len({c.block_id for f in facts for c in f.versions[0].citations}) == 2
    assert {f.versions[0].classification for f in facts} == {"as_is"}
    assert run.coverage == "incomplete" and run.status == "partial"
    assert next(f for f in run.findings if f.kind == "conflict").status == "open"

def test_repeat_run_does_not_duplicate_facts(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide])
    config = LLMConfig("test-credential", "fixture/flash", "fixture/pro")
    ExtractionPipeline(store, client, config).run([sparse_slide])
    assert len(store.load().facts) == 4
    assert all(len(f.versions) == 1 for f in store.load().facts.values())
    assert len(store.load().runs) == 2

@pytest.mark.parametrize("mode", ["invalid_quote", "audit_error", "unsupported"])
def test_invalid_output_or_capability_never_claims_coverage(tmp_path, sparse_slide, mode):
    store, client, run = execute(tmp_path, [sparse_slide], mode)
    assert run.status == "partial" and run.coverage == "incomplete"
    if mode == "invalid_quote":
        assert not store.load().facts
        assert any(f.kind == "invalid_output" for f in run.findings)
    if mode == "unsupported":
        assert not any(c[0] == "process_evidence" for c in client.calls)
    assert not any(e.get("status") == "pending" for e in run.events)

def test_partial_parsing_keeps_good_document_and_reports_gap(tmp_path, sparse_slide):
    bad = tmp_path / "bad.pdf"; bad.write_bytes(b"corrupt")
    store, client, run = execute(tmp_path, [sparse_slide, bad])
    assert len(store.load().facts) == 4
    assert run.status == "partial" and run.coverage == "incomplete"
    assert any(f.kind == "parsing_gap" for f in run.findings)
    json_path, report = write_report(store, run.id, tmp_path / "output")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["documents"]) == 2 and payload["facts"]
    assert "bad.pdf" in report.read_text(encoding="utf-8")

def test_call_budget_produces_explicit_incomplete_result(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide], max_calls=2)
    assert run.errors and run.coverage == "incomplete"
    assert not store.load().facts

def test_task_model_overrides_and_independence(tmp_path, sparse_slide):
    store, client, run = execute(tmp_path, [sparse_slide], extraction_model="fixture/alternate-flash", audit_model="fixture/alternate-pro")
    assert ("verify", "fixture/alternate-flash") in client.calls
    assert ("verify", "fixture/alternate-pro") in client.calls
    store2, client2, run2 = execute(tmp_path / "other", [sparse_slide], extraction_model="fixture/pro")
    assert run2.coverage == "incomplete" and "different effective model" in run2.errors[0]
    assert not client2.calls

def test_split_sources_are_all_attempted_and_cross_batch_gap_explicit(tmp_path, detailed_sop):
    store, client, run = execute(tmp_path, [detailed_sop], batch_chars=100)
    extraction = [c for c in client.calls if c[0] == "process_evidence"]
    seen = [(v["id"], v["start"], v["end"]) for c in extraction for v in c[2]["source_views"]]
    for block in store.load().blocks.values():
        spans = sorted((s, e) for id, s, e in seen if id == block.id)
        assert spans[0][0] == 0 and spans[-1][1] == len(block.text)
        assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    assert any("cross-batch" in f.description for f in run.findings)
    assert run.coverage == "incomplete"

def test_cli_parse_only_is_offline(tmp_path, detailed_sop, monkeypatch, capsys):
    from bpt_process_agent.ingestion.cli import main
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code = main([str(detailed_sop), "--parse-only", "--store", str(tmp_path / "cli-store"), "--output", str(tmp_path / "output")])
    assert code == 0
    state = EvidenceStore(tmp_path / "cli-store").load()
    assert state.documents and not state.facts and not next(iter(state.runs.values())).call_ids
    assert (tmp_path / "output" / "evidence.json").is_file()

def test_cli_mocked_full_extraction(tmp_path, sparse_slide, monkeypatch):
    from bpt_process_agent.ingestion import cli
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")
    monkeypatch.setenv("OPENROUTER_FLASH_MODEL", "fixture/flash")
    monkeypatch.setenv("OPENROUTER_PRO_MODEL", "fixture/pro")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "OpenRouterClient", lambda config: ScriptedClient())
    assert cli.main([str(sparse_slide), "--store", str(tmp_path / "cli"), "--output", str(tmp_path / "output")]) == 0

def test_graph_state_is_untouched(tmp_path, sparse_slide):
    graph = tmp_path / "graph.json"; graph.write_text("canonical graph fixture", encoding="utf-8")
    execute(tmp_path, [sparse_slide])
    assert graph.read_text(encoding="utf-8") == "canonical graph fixture"
