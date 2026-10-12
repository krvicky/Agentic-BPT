"""Policy gates and transitions; semantic assessment is supplied, never inferred here."""
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Protocol
from .schemas import (Assumption, Boundary, BoundaryAssessment, BoundaryChange,
                      ASSUMPTION_TRANSITIONS, Evidence, Graph, Operation, Question, RECORD_TYPES,
                      Resolution, decode_record, matches_schema)

class PolicyViolation(ValueError):
    pass

class AmbiguousAlias(PolicyViolation):
    pass

class BoundaryReviewer(Protocol):
    """Future semantic-review integration. No implementation or API calls."""
    def assess(self, proposal: dict, boundary: Boundary,
               evidence: dict[str, Evidence]) -> BoundaryAssessment: ...

def proposal_digest(proposal: Operation | Question) -> str:
    value = asdict(proposal) if is_dataclass(proposal) else proposal
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()

def check_assessment(graph: Graph, proposal: Operation | Question,
                     assessment: BoundaryAssessment) -> None:
    if not matches_schema(assessment, BoundaryAssessment):
        raise PolicyViolation("Assessment schema is invalid")
    if assessment.status == "out_of_scope":
        raise PolicyViolation("Proposal explicitly assessed out of scope")
    b = graph.boundary
    if assessment.status != "in_scope":
        raise PolicyViolation("A completed in-scope assessment is required")
    if (assessment.boundary_id != b.id or assessment.boundary_revision != b.revision
            or assessment.start_trigger != b.start_trigger or assessment.end_condition != b.end_condition):
        raise PolicyViolation("Assessment refers to a stale or different boundary")
    if not assessment.id.strip() or not assessment.reviewer.strip() or not assessment.scope_justification.strip():
        raise PolicyViolation("Assessment requires identity, reviewer and scope justification")
    if assessment.scope_id not in b.scope_ids or assessment.proposal_digest != proposal_digest(proposal):
        raise PolicyViolation("Assessment scope or exact proposal binding is invalid")
    if isinstance(proposal, Question) and assessment.scope_id != proposal.scope_id:
        raise PolicyViolation("Question and assessment scope must match")
    if isinstance(proposal, Operation) and proposal.collection in ("nodes", "flows") and proposal.action != "remove":
        if not proposal.value or proposal.value.get("scope_id") != assessment.scope_id:
            raise PolicyViolation("Element and assessment scope must match")
    if not assessment.evidence_ids or any(
            eid not in graph.evidence or graph.evidence[eid].classification != "as_is"
            for eid in assessment.evidence_ids):
        raise PolicyViolation("Assessment requires registered As-Is evidence")

def check_question(graph: Graph, question: Question, assessment: BoundaryAssessment) -> None:
    if not matches_schema(question, Question) or not question.id.strip() or not question.text.strip():
        raise PolicyViolation("Question schema, identity and text are required")
    check_assessment(graph, question, assessment)
    if question.kind == "start_boundary" and graph.boundary.start_resolved:
        raise PolicyViolation("Start boundary-definition questions require an unresolved start")
    if question.kind == "end_boundary" and graph.boundary.end_resolved:
        raise PolicyViolation("End boundary-definition questions require an unresolved end")

def resolve_persona_alias(graph: Graph, alias: str, context: str | None = None) -> str:
    normalized = alias.strip().casefold()
    candidates = [
        p.id for p in graph.personas.values()
        if (context is None or p.organizational_context.strip().casefold() == context.strip().casefold())
        and normalized in {x.strip().casefold() for x in [p.label, *p.aliases]}
    ]
    if len(candidates) != 1:
        raise AmbiguousAlias("Alias is ambiguous or unknown; explicit persona identity is required")
    return candidates[0]

def _require_support(graph: Graph, value: dict) -> None:
    ids, confirmation = value.get("evidence_ids", []), value.get("confirmation_id")
    if not isinstance(ids, list) or any(
            not isinstance(eid, str) or eid not in graph.evidence or graph.evidence[eid].classification != "as_is"
            for eid in ids):
        raise PolicyViolation("Resolution evidence must be registered As-Is evidence")
    if confirmation is not None and (not isinstance(confirmation, str) or confirmation not in graph.confirmations):
        raise PolicyViolation("Resolution confirmation must already be recorded")
    if not ids and confirmation is None:
        raise PolicyViolation("Resolution needs evidence or a recorded user confirmation")
    if not isinstance(value.get("rationale"), str) or not value["rationale"].strip():
        raise PolicyViolation("Resolution rationale is required")

def apply_operation(graph: Graph, op: Operation, *, origin: str, lineage: dict) -> None:
    """Mutate only an isolated candidate. Caller validates and atomically commits."""
    if not matches_schema(op, Operation) or not op.id.strip():
        raise PolicyViolation("Malformed operation")
    if op.action == "resolve_assumption":
        if op.collection != "assumptions" or op.id not in graph.assumptions or not isinstance(op.value, dict):
            raise PolicyViolation("Assumption resolution requires an existing assumption")
        assumption = graph.assumptions[op.id]
        resolution = decode_record(Resolution, op.value)
        if resolution.status not in ASSUMPTION_TRANSITIONS[assumption.status]:
            raise PolicyViolation("Unsupported assumption lifecycle transition")
        _require_support(graph, op.value)
        resolution.previous_status = assumption.status
        assumption.history.append(resolution)
        assumption.status = resolution.status
        return
    if op.action in ("confirm_boundary", "revise_boundary"):
        if op.collection != "boundary" or op.id != graph.boundary.id or not isinstance(op.value, dict):
            raise PolicyViolation("Boundary operation must target the current contract")
        _require_support(graph, op.value)
        b = graph.boundary
        allowed = {"rationale", "evidence_ids", "confirmation_id"}
        if op.action == "revise_boundary":
            allowed |= {"start_trigger", "end_condition", "start_resolved", "end_resolved", "scope_ids", "confidence"}
        if set(op.value) - allowed:
            raise PolicyViolation("Unsupported boundary change fields")
        previous = deepcopy(b)
        for key in allowed - {"rationale", "confirmation_id", "evidence_ids"}:
            if key in op.value:
                setattr(b, key, deepcopy(op.value[key]))
        b.evidence_ids = list(dict.fromkeys([*b.evidence_ids, *op.value.get("evidence_ids", [])]))
        if op.action == "confirm_boundary" and not (b.start_resolved and b.end_resolved):
            raise PolicyViolation("Cannot confirm an unresolved boundary")
        b.status = "confirmed" if op.action == "confirm_boundary" else "provisional"
        b.revision += 1
        b.history.append(BoundaryChange(b.revision, op.action, previous.start_trigger,
                                       previous.end_condition, previous.status,
                                       op.value["rationale"], op.value.get("evidence_ids", []),
                                       op.value.get("confirmation_id")))
        graph.complete = False
        return
    if op.action == "set_graph_state":
        if op.collection != "graph" or op.id != graph.id or not isinstance(op.value, dict) or not op.value or set(op.value) - {"complete", "unresolved_issues", "pool_ids"}:
            raise PolicyViolation("Graph state operation fields are invalid")
        for key, value in op.value.items():
            setattr(graph, key, deepcopy(value))
        return
    if op.collection not in RECORD_TYPES or op.action not in ("add", "update", "remove"):
        raise PolicyViolation("Unsupported registry operation")
    records = getattr(graph, op.collection)
    if op.action == "add" and op.id in records or op.action != "add" and op.id not in records:
        raise PolicyViolation("Operation existence precondition failed")
    if op.collection == "confirmations":
        if origin != "manual" or op.action != "add":
            raise PolicyViolation("Only trusted callers can append user confirmations")
    if op.collection == "assumptions" and op.action != "add":
        raise PolicyViolation("Assumptions require explicit resolution; originals cannot be updated or deleted")
    if op.collection == "evidence" and op.action == "remove":
        raise PolicyViolation("Evidence registry is append-only")
    if op.action == "remove":
        if op.value is not None:
            raise PolicyViolation("Remove cannot carry a payload")
        del records[op.id]
        return
    if not isinstance(op.value, dict):
        raise PolicyViolation("Operation payload must be an object")
    record = decode_record(RECORD_TYPES[op.collection], op.value)
    if record.id != op.id:
        raise PolicyViolation("Record identity mismatch")
    if op.collection == "assumptions" and (record.status != "unverified" or record.history):
        raise PolicyViolation("New assumptions must be unverified with no fabricated resolution history")
    if op.collection == "evidence" and op.action == "update" and record != records[op.id]:
        raise PolicyViolation("Evidence is immutable")
    if op.collection in ("nodes", "flows", "personas"):
        original = lineage.get((op.collection, op.id))
        if original is not None and record.assumption_id != original:
            raise PolicyViolation("Original assumption lineage cannot be removed or reassigned")
        if record.assumption_id is not None:
            lineage[(op.collection, op.id)] = record.assumption_id
    records[op.id] = record
