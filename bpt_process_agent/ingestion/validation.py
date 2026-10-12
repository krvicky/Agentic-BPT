"""Local output/citation validation. Semantic support remains an audit assertion."""
from dataclasses import asdict
import re
from jsonschema import Draft202012Validator
from .models import (AuditFinding, CandidateFact, Citation, Registry, TAGS, fact_id, stable_id)
from .parsers import block_id
from ..schemas import decode_record, matches_schema
from ..tracing import contains_secret

def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

CITATION_SCHEMA = obj({
    "block_id": {"type": "string", "minLength": 1},
    "start": {"type": "integer", "minimum": 0},
    "end": {"type": "integer", "minimum": 1},
    "quote": {"type": "string", "minLength": 1},
})
TAG_SCHEMA = {"type": "array", "items": {"type": "string", "enum": TAGS}, "minItems": 1, "uniqueItems": True}
CLASS_SCHEMA = {"type": "string", "enum": ["as_is", "to_be", "ambiguous"]}
FACT_SCHEMA = obj({
    "statement": {"type": "string", "minLength": 1},
    "tags": TAG_SCHEMA, "classification": CLASS_SCHEMA,
    "citations": {"type": "array", "items": CITATION_SCHEMA, "minItems": 1},
    "ambiguity": {"type": "string"},
})
EXTRACTION_SCHEMA = obj({"facts": {"type": "array", "items": FACT_SCHEMA}})
FINDING_SCHEMA = obj({
    "kind": {"type": "string", "enum": ["missing", "unsupported", "incorrect_classification", "conflict", "ambiguous", "parsing_gap"]},
    "description": {"type": "string", "minLength": 1},
    "fact_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    "citations": {"type": "array", "items": CITATION_SCHEMA},
    "suggested_classification": {"anyOf": [CLASS_SCHEMA, {"type": "null"}]},
    "suggested_tags": {"anyOf": [TAG_SCHEMA, {"type": "null"}]},
})
AUDIT_SCHEMA = obj({
    "findings": {"type": "array", "items": FINDING_SCHEMA},
    "resolved_finding_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
})

class EvidenceValidationError(ValueError):
    pass

def validate_json(value: dict, schema: dict) -> None:
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise EvidenceValidationError("Structured response does not satisfy the declared schema")
    if contains_secret(value):
        raise EvidenceValidationError("Sensitive content is prohibited in structured evidence")

def validate_citations(citations: list[Citation], registry: Registry, allowed_blocks: set[str] | None = None) -> None:
    if not citations:
        raise EvidenceValidationError("A source citation is required")
    seen = set()
    for c in citations:
        if not matches_schema(c, Citation):
            raise EvidenceValidationError("Citation schema is invalid")
        block = registry.blocks.get(c.block_id)
        if block is None or allowed_blocks is not None and c.block_id not in allowed_blocks:
            raise EvidenceValidationError("Citation refers to an unavailable or out-of-batch source block")
        if not 0 <= c.start < c.end <= len(block.text) or block.text[c.start:c.end] != c.quote:
            raise EvidenceValidationError("Supporting passage must exactly match the parsed source span")
        key = (c.block_id, c.start, c.end)
        if key in seen:
            raise EvidenceValidationError("Duplicate supporting citation")
        seen.add(key)

def validate_candidate(candidate: CandidateFact, registry: Registry, allowed_blocks: set[str] | None = None) -> None:
    if not matches_schema(candidate, CandidateFact) or not candidate.statement.strip() or not candidate.tags or len(candidate.tags) != len(set(candidate.tags)):
        raise EvidenceValidationError("Fact statement, tags or types are invalid")
    if candidate.classification == "ambiguous" and not candidate.ambiguity.strip():
        raise EvidenceValidationError("Ambiguous statements must preserve their uncertainty")
    if contains_secret(candidate):
        raise EvidenceValidationError("Sensitive content is prohibited")
    validate_citations(candidate.citations, registry, allowed_blocks)

def extraction_candidates(value: dict, registry: Registry, allowed: set[str]) -> list[CandidateFact]:
    validate_json(value, EXTRACTION_SCHEMA)
    candidates = [decode_record(CandidateFact, item) for item in value["facts"]]
    for candidate in candidates:
        validate_candidate(candidate, registry, allowed)
    return candidates

def audit_findings(value: dict, registry: Registry, allowed_blocks: set[str], allowed_facts: set[str],
                   run_id: str, call_id: str) -> tuple[list[AuditFinding], list[str]]:
    validate_json(value, AUDIT_SCHEMA)
    findings = []
    for item in value["findings"]:
        citations = [decode_record(Citation, c) for c in item["citations"]]
        if citations:
            validate_citations(citations, registry, allowed_blocks)
        if any(id not in allowed_facts for id in item["fact_ids"]):
            raise EvidenceValidationError("Audit finding refers to an unavailable fact")
        kind = item["kind"]
        if kind in ("missing", "incorrect_classification", "conflict", "ambiguous") and not citations:
            raise EvidenceValidationError("This audit finding requires exact source passages")
        if kind in ("unsupported", "incorrect_classification") and not item["fact_ids"]:
            raise EvidenceValidationError("Audit finding must identify the affected fact")
        if kind == "conflict" and len(item["fact_ids"]) < 2:
            raise EvidenceValidationError("A conflict must retain at least two facts")
        if kind == "incorrect_classification" and item["suggested_classification"] is None:
            raise EvidenceValidationError("Classification correction must specify its proposed class")
        id = stable_id("finding", run_id, kind, item["fact_ids"], [asdict(c) for c in citations], item["description"])
        findings.append(AuditFinding(id, kind, item["description"], item["fact_ids"], citations,
                                     item["suggested_classification"], item["suggested_tags"], call_id=call_id))
    return findings, value["resolved_finding_ids"]

def validate_registry(registry: Registry) -> None:
    if not matches_schema(registry, Registry) or registry.version < 0 or contains_secret(registry):
        raise EvidenceValidationError("Evidence registry types, version or sensitive-content policy are invalid")
    for id, doc in registry.documents.items():
        if id != doc.id or not doc.filename or len(doc.block_ids) != len(set(doc.block_ids)):
            raise EvidenceValidationError("Invalid document identity or block registry")
        if doc.sha256 and (not re.fullmatch(r"[0-9a-f]{64}", doc.sha256) or id != stable_id("doc", doc.sha256, doc.format)):
            raise EvidenceValidationError("Document content identity is invalid")
        if not doc.sha256 and id != stable_id("unreadable", doc.filename):
            raise EvidenceValidationError("Unreadable document identity is invalid")
        if doc.archive_name and doc.archive_name != "sources/" + id + "." + (doc.format if doc.format in ("pdf", "pptx", "docx") else "bin"):
            raise EvidenceValidationError("Source archive path is invalid")
        if not doc.archive_name and doc.status != "failed":
            raise EvidenceValidationError("A usable document needs its archived original")
        if any(b not in registry.blocks or registry.blocks[b].document_id != id for b in doc.block_ids):
            raise EvidenceValidationError("Document has dangling source blocks")
        if doc.status == "parsed" and (not doc.block_ids or any(i.affects_coverage for i in doc.issues)):
            raise EvidenceValidationError("Incomplete parsing cannot be labeled parsed")
        if doc.status == "failed" and doc.block_ids:
            raise EvidenceValidationError("Failed documents cannot contain usable blocks")
    for id, block in registry.blocks.items():
        if id != block.id or not block.text.strip() or block.document_id not in registry.documents or id not in registry.documents[block.document_id].block_ids:
            raise EvidenceValidationError("Invalid immutable source block")
        if id != block_id(block.document_id, block.kind, block.locator, block.text, block.rows, block.page, block.slide, block.section):
            raise EvidenceValidationError("Source block content identity mismatch")
        format = registry.documents[block.document_id].format
        if format == "pdf" and (block.page is None or block.page < 1 or block.slide is not None) or format == "pptx" and (block.slide is None or block.slide < 1 or block.page is not None) or format == "docx" and (block.page is not None or block.slide is not None):
            raise EvidenceValidationError("Source format locator is invalid")
        if block.kind == "table" and block.text != "\n".join("\t".join(row) for row in block.rows):
            raise EvidenceValidationError("Table text must match its structured rows")
    for id, fact in registry.facts.items():
        if id != fact.id or not fact.versions or id != fact_id(fact.versions[0].citations):
            raise EvidenceValidationError("Fact identity does not match its original source anchors")
        for index, version in enumerate(fact.versions, 1):
            if version.revision != index or version.run_id not in registry.runs or not version.reason.strip():
                raise EvidenceValidationError("Fact history revision or provenance is invalid")
            validate_candidate(CandidateFact(version.statement, version.tags, version.classification, version.citations, version.ambiguity), registry)
            if index > 1 or version.audit_finding_id:
                finding = next((f for f in registry.runs[version.run_id].findings if f.id == version.audit_finding_id), None)
                if finding is None:
                    raise EvidenceValidationError("Evidence revisions/recoveries require an existing explicit audit finding")
                if index > 1:
                    old = fact.versions[index - 2]
                    if id not in finding.fact_ids or finding.kind not in ("unsupported", "incorrect_classification"):
                        raise EvidenceValidationError("Revision is not bound to a relevant correction")
                    if version.statement != old.statement or version.citations != old.citations:
                        raise EvidenceValidationError("Classification/quarantine corrections cannot rewrite source assertions")
                    if finding.kind == "unsupported" and (version.status != "quarantined" or version.tags != old.tags or version.classification != old.classification):
                        raise EvidenceValidationError("Unsupported correction must quarantine the unchanged assertion")
                    if finding.kind == "incorrect_classification" and (
                            version.classification != finding.suggested_classification
                            or version.tags != (finding.suggested_tags or old.tags) or version.status != old.status):
                        raise EvidenceValidationError("Correction does not match its audit finding")
                elif finding.kind != "missing":
                    raise EvidenceValidationError("New recovered facts require an omission finding")
    for id, run in registry.runs.items():
        if id != run.id or any(d not in registry.documents for d in run.document_ids):
            raise EvidenceValidationError("Extraction run identity or source references are invalid")
        if run.input_documents != [{"filename": name, "document_id": item.get("document_id")} for name, item in zip(run.inputs, run.input_documents)] or len(run.inputs) != len(run.input_documents) or any(item.get("document_id") not in run.document_ids for item in run.input_documents):
            raise EvidenceValidationError("Uploaded filename mapping is invalid")
        if len(run.findings) != len({f.id for f in run.findings}) or len(run.call_ids) != len(set(run.call_ids)):
            raise EvidenceValidationError("Duplicate finding or model call identity")
        calls = [e for e in run.events if e.get("type") == "model_call"]
        if [e.get("id") for e in calls] != run.call_ids:
            raise EvidenceValidationError("Model call registry does not match its events")
        for event in calls:
            if event.get("status") not in ("pending", "completed", "failed") or event.get("task") not in ("extraction", "evidence_audit", "capability_check"):
                raise EvidenceValidationError("Model call state is invalid")
        for finding in run.findings:
            if finding.suggested_classification not in (None, "as_is", "to_be", "ambiguous"):
                raise EvidenceValidationError("Suggested classification is invalid")
            if finding.citations:
                validate_citations(finding.citations, registry, {b.id for b in registry.blocks.values() if b.document_id in run.document_ids})
            if finding.kind in ("missing", "incorrect_classification", "conflict", "ambiguous") and not finding.citations:
                raise EvidenceValidationError("Finding lacks required exact source support")
            if finding.kind in ("unsupported", "incorrect_classification") and not finding.fact_ids:
                raise EvidenceValidationError("Finding lacks its affected fact")
            if finding.kind == "conflict" and len(set(finding.fact_ids)) < 2:
                raise EvidenceValidationError("Conflict needs two distinct source assertions")
            if any(f not in registry.facts for f in finding.fact_ids):
                raise EvidenceValidationError("Audit history references unknown facts")
        if run.coverage == "audited_parsed_content" and (
                any(registry.documents[d].status != "parsed" for d in run.document_ids)
                or run.errors or any(f.status != "resolved" for f in run.findings)):
            raise EvidenceValidationError("Partial or unresolved evidence cannot claim an audited clean result")

    if len(registry.trace) != registry.version:
        raise EvidenceValidationError("Evidence version and trace length disagree")
    trace_ids = set()
    for index, event in enumerate(registry.trace):
        if (event.get("before_version") != index or event.get("after_version") != index + 1
                or not isinstance(event.get("id"), str) or not event["id"] or event["id"] in trace_ids
                or event.get("outcome") not in ("accepted", "rejected") or event.get("graph_version") is not None):
            raise EvidenceValidationError("Evidence trace identity, outcome or versions are invalid")
        trace_ids.add(event["id"])
