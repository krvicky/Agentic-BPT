"""Typed records and public runtime schema checks for the local prototype."""
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from functools import lru_cache
from types import UnionType
from typing import Literal, get_args, get_origin, get_type_hints

@lru_cache
def _hints(record: type) -> dict:
    return get_type_hints(record)

def matches_schema(value: object, annotation: object) -> bool:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return any(type(value) is type(arg) and value == arg for arg in args)
    if origin is UnionType:
        return any(matches_schema(value, arg) for arg in args)
    if origin is list:
        return isinstance(value, list) and all(matches_schema(v, args[0]) for v in value)
    if origin is dict:
        return isinstance(value, dict) and all(matches_schema(k, args[0]) and matches_schema(v, args[1]) for k, v in value.items())
    if annotation is float:
        return type(value) in (float, int)
    if is_dataclass(annotation):
        return isinstance(value, annotation) and all(matches_schema(getattr(value, f.name), _hints(annotation)[f.name]) for f in fields(annotation))
    return type(value) is annotation

def decode_record(record: type, value: dict):
    if not isinstance(value, dict):
        raise ValueError("Record must be an object")
    data = dict(value)
    for f in fields(record):
        if f.name not in data:
            continue
        annotation = _hints(record)[f.name]
        origin, args = get_origin(annotation), get_args(annotation)
        if is_dataclass(annotation):
            data[f.name] = decode_record(annotation, data[f.name])
        elif origin is list and is_dataclass(args[0]):
            data[f.name] = [decode_record(args[0], item) for item in data[f.name]]
        elif origin is dict and is_dataclass(args[1]):
            data[f.name] = {key: decode_record(args[1], item) for key, item in data[f.name].items()}
    result = record(**data)
    if not matches_schema(result, record):
        raise ValueError("Record types or enum values are invalid")
    return result

@dataclass
class Evidence:
    id: str
    source_id: str
    source_text: str
    start: int
    end: int
    quote: str
    classification: Literal["as_is", "to_be", "problem", "context"] = "as_is"

@dataclass
class UserConfirmation:
    id: str
    statement: str
    confirmed_by: str
    recorded_at: str

@dataclass
class Resolution:
    id: str
    status: Literal["confirmed", "revised", "rejected"]
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    confirmation_id: str | None = None
    previous_status: str = "unverified"

@dataclass
class BoundaryChange:
    revision: int
    action: Literal["confirm_boundary", "revise_boundary"]
    previous_start: str
    previous_end: str
    previous_status: str
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    confirmation_id: str | None = None

@dataclass
class Boundary:
    id: str
    start_trigger: str
    end_condition: str
    status: Literal["confirmed", "provisional"]
    confidence: float
    evidence_ids: list[str]
    scope_ids: list[str]
    start_resolved: bool = True
    end_resolved: bool = True
    revision: int = 0
    history: list[BoundaryChange] = field(default_factory=list)

@dataclass
class BoundaryAssessment:
    id: str
    boundary_id: str
    boundary_revision: int
    start_trigger: str
    end_condition: str
    scope_id: str
    scope_justification: str
    status: Literal["pending", "in_scope", "out_of_scope", "needs_review"]
    evidence_ids: list[str]
    proposal_digest: str
    reviewer: str
    method: Literal["human", "semantic_reviewer"] = "human"

@dataclass
class Persona:
    id: str
    label: str
    aliases: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    assumption_id: str | None = None
    organizational_context: str = ""

@dataclass
class Lane:
    id: str
    pool_id: str
    persona_id: str

@dataclass
class Assumption:
    id: str
    rationale: str
    evidence_ids: list[str]
    status: Literal["unverified", "confirmed", "revised", "rejected"] = "unverified"
    history: list[Resolution] = field(default_factory=list)

@dataclass
class Node:
    id: str
    label: str
    kind: Literal["start", "activity", "gateway", "end"]
    scope_id: str
    lane_id: str
    evidence_ids: list[str] = field(default_factory=list)
    assumption_id: str | None = None
    parent_id: str | None = None
    level: Literal["L1", "L2", "L3"] = "L1"

@dataclass
class Flow:
    id: str
    source_id: str
    target_id: str
    scope_id: str
    evidence_ids: list[str] = field(default_factory=list)
    assumption_id: str | None = None

@dataclass
class Graph:
    id: str
    boundary: Boundary
    version: int = 0
    evidence: dict[str, Evidence] = field(default_factory=dict)
    assumptions: dict[str, Assumption] = field(default_factory=dict)
    personas: dict[str, Persona] = field(default_factory=dict)
    lanes: dict[str, Lane] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    flows: dict[str, Flow] = field(default_factory=dict)
    pool_ids: list[str] = field(default_factory=list)
    complete: bool = False
    unresolved_issues: list[str] = field(default_factory=list)
    confirmations: dict[str, UserConfirmation] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Graph":
        return decode_record(cls, value)

RECORD_TYPES = {"evidence": Evidence, "assumptions": Assumption,
                "personas": Persona, "lanes": Lane, "nodes": Node, "flows": Flow,
                "confirmations": UserConfirmation}

@dataclass(frozen=True)
class Operation:
    action: Literal["add", "update", "remove", "resolve_assumption", "confirm_boundary", "revise_boundary", "set_graph_state"]
    collection: Literal["evidence", "assumptions", "personas", "lanes", "nodes", "flows", "confirmations", "boundary", "graph"]
    id: str
    value: dict | None = None

@dataclass
class Question:
    id: str
    text: str
    scope_id: str
    kind: Literal["activity", "start_boundary", "end_boundary"] = "activity"

@dataclass
class PendingQuestion:
    question: Question
    assessment: BoundaryAssessment
    graph_version: int

@dataclass
class Checkpoint:
    id: str
    graph_version: int
    note: str = ""

@dataclass
class ExecutionState:
    run_id: str
    version: int = 0
    graph_version: int = 0
    status: Literal["ready", "running", "paused", "complete", "blocked"] = "ready"
    budgets: dict[str, int] = field(default_factory=dict)
    pending_questions: list[PendingQuestion] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)

@dataclass(frozen=True)
class Finding:
    code: str
    element_id: str
    message: str
    category: str = "structural"

# Explicit supported transitions; reopening is recorded, never a generic edit.
ASSUMPTION_TRANSITIONS = {
    "unverified": {"confirmed", "revised", "rejected"},
    "revised": {"confirmed", "revised", "rejected"},
    "confirmed": {"revised", "rejected"},
    "rejected": {"revised"},
}
