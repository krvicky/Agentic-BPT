"""Evidence-ingestion records are independent of canonical graph records."""
from dataclasses import asdict, dataclass, field
import hashlib
from typing import Literal
from ..schemas import decode_record
from ..tracing import canonical_json

Tag = Literal["activity", "role", "decision", "rule", "exception", "system", "document", "SLA", "pain_point", "other"]
Classification = Literal["as_is", "to_be", "ambiguous"]
TAGS = ["activity", "role", "decision", "rule", "exception", "system", "document", "SLA", "pain_point", "other"]

def stable_id(prefix: str, *values: object) -> str:
    return prefix + "_" + hashlib.sha256(canonical_json(values).encode()).hexdigest()

@dataclass
class ParseIssue:
    code: str
    message: str
    locator: str
    affects_coverage: bool = True

@dataclass
class SourceBlock:
    id: str
    document_id: str
    kind: Literal["text", "table", "notes", "header", "footer"]
    locator: str
    text: str
    page: int | None = None
    slide: int | None = None
    section: str = ""
    rows: list[list[str]] = field(default_factory=list)

@dataclass
class SourceDocument:
    id: str
    sha256: str
    filename: str
    format: str
    archive_name: str
    status: Literal["parsed", "partial", "failed"]
    block_ids: list[str]
    issues: list[ParseIssue]
    parser_version: str = "1"

@dataclass
class ParsedDocument:
    document: SourceDocument
    blocks: list[SourceBlock]

@dataclass
class Citation:
    block_id: str
    start: int
    end: int
    quote: str

@dataclass
class CandidateFact:
    statement: str
    tags: list[Tag]
    classification: Classification
    citations: list[Citation]
    ambiguity: str

def fact_id(citations: list[Citation]) -> str:
    return stable_id("fact", sorted({(c.block_id, c.start, c.end) for c in citations}))

@dataclass
class FactVersion:
    revision: int
    statement: str
    tags: list[Tag]
    classification: Classification
    citations: list[Citation]
    ambiguity: str
    status: Literal["active", "quarantined"]
    run_id: str
    reason: str
    audit_finding_id: str | None = None

@dataclass
class FactRecord:
    id: str
    versions: list[FactVersion]

@dataclass
class AuditFinding:
    id: str
    kind: Literal["missing", "unsupported", "incorrect_classification", "conflict", "ambiguous", "parsing_gap", "invalid_output", "repeat_disagreement"]
    description: str
    fact_ids: list[str]
    citations: list[Citation]
    suggested_classification: str | None
    suggested_tags: list[Tag] | None
    status: Literal["open", "recovery_attempted", "resolved"] = "open"
    call_id: str = ""

@dataclass
class ExtractionRun:
    id: str
    document_ids: list[str]
    inputs: list[str]
    started_at: str
    finished_at: str = ""
    status: Literal["running", "failed", "partial", "audited"] = "running"
    coverage: Literal["not_audited", "incomplete", "audited_parsed_content"] = "not_audited"
    findings: list[AuditFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    input_documents: list[dict] = field(default_factory=list)

@dataclass
class Registry:
    version: int = 0
    documents: dict[str, SourceDocument] = field(default_factory=dict)
    blocks: dict[str, SourceBlock] = field(default_factory=dict)
    facts: dict[str, FactRecord] = field(default_factory=dict)
    runs: dict[str, ExtractionRun] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Registry":
        return decode_record(cls, value)
