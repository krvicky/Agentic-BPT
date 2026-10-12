"""Local graph transactions, independent execution state, validated snapshots and audit."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator
from uuid import uuid4
from .schemas import (BoundaryAssessment, ExecutionState, Finding, Graph, Operation,
                      PendingQuestion, Question, decode_record, matches_schema)
from .validation import validate
from .policy import PolicyViolation, apply_operation, check_assessment, check_question, proposal_digest
from .tracing import canonical_json, contains_secret, redact, safe_digest

class StoreError(Exception):
    """Base class for local transaction failures."""

class VersionConflict(StoreError):
    pass

class StoreBusy(StoreError):
    pass

class CorruptState(StoreError):
    pass

class MutationRejected(StoreError):
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("Mutation rejected: " + ", ".join(f.code for f in findings))

def _lineage(versions: list[dict]) -> dict:
    result = {}
    for version in versions:
        for collection in ("personas", "nodes", "flows"):
            for id, value in version[collection].items():
                assumption = value.get("assumption_id")
                if assumption is not None:
                    key = (collection, id)
                    if key in result and result[key] != assumption:
                        raise ValueError("Assumption lineage changed")
                    result[key] = assumption
                elif (collection, id) in result:
                    raise ValueError("Assumption lineage was removed")
    return result

def _execution_findings(state: ExecutionState, graph: Graph) -> list[Finding]:
    if not matches_schema(state, ExecutionState):
        return [Finding("execution_schema", "run", "Execution state schema is invalid")]
    issues = []
    if not state.run_id.strip() or state.version < 0 or state.graph_version != graph.version:
        issues.append(Finding("execution_version", state.run_id, "Execution state must target the current graph version"))
    if any(not key.strip() or remaining < 0 for key, remaining in state.budgets.items()):
        issues.append(Finding("execution_budget", state.run_id, "Remaining budgets must be nonnegative"))
    ids = [c.id for c in state.checkpoints]
    if len(ids) != len(set(ids)) or any(not c.id.strip() or not 0 <= c.graph_version <= graph.version for c in state.checkpoints):
        issues.append(Finding("checkpoint", state.run_id, "Checkpoint IDs and graph versions are invalid"))
    question_ids = [p.question.id for p in state.pending_questions]
    if len(question_ids) != len(set(question_ids)):
        issues.append(Finding("question_identity", state.run_id, "Pending question IDs must be unique"))
    for pending in state.pending_questions:
        try:
            if pending.graph_version != graph.version:
                raise PolicyViolation("Pending question targets a stale graph")
            check_question(graph, pending.question, pending.assessment)
        except PolicyViolation:
            issues.append(Finding("question_boundary", pending.question.id, "Pending question boundary assessment is invalid"))
    return issues

class GraphStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.snapshot_path = self.path.with_name(self.path.name + ".snapshot.json")
        self.failure_path = self.path.with_name(self.path.name + ".failures.jsonl")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StoreBusy("Store is locked; inspect the lock owner before recovery") from exc
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(canonical_json({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}))
            yield
        finally:
            self.lock_path.unlink()

    @staticmethod
    def _empty() -> dict:
        return {"format_version": 2, "graph": None, "versions": [], "trace": [], "execution_state": None}

    @staticmethod
    def _validate_envelope(envelope: dict) -> dict:
        try:
            if not isinstance(envelope, dict) or set(envelope) - {"format_version", "graph", "versions", "trace", "execution_state", "checksum"} or not {"format_version", "graph", "versions", "trace"} <= set(envelope) or type(envelope.get("format_version")) is not int or envelope["format_version"] not in (1, 2):
                raise ValueError("Unsupported state format")
            legacy = envelope["format_version"] == 1
            if not legacy:
                payload = {key: value for key, value in envelope.items() if key != "checksum"}
                expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if envelope.get("checksum") != expected:
                    raise ValueError("State checksum does not match")
            if not isinstance(envelope.get("versions"), list) or not isinstance(envelope.get("trace"), list):
                raise ValueError("History and trace must be arrays")
            normalized = deepcopy(envelope)
            normalized.pop("checksum", None)
            normalized["format_version"] = 2
            normalized.setdefault("execution_state", None)
            versions = []
            previous = None
            for index, value in enumerate(envelope["versions"]):
                graph = Graph.from_dict(value)
                if validate(graph) or graph.version != index or contains_secret(graph):
                    raise ValueError("Invalid graph history")
                if previous:
                    if graph.id != previous.id or graph.boundary.id != previous.boundary.id:
                        raise ValueError("Canonical identity changed")
                    for collection in ("evidence", "confirmations"):
                        for id, record in getattr(previous, collection).items():
                            if getattr(graph, collection).get(id) != record:
                                raise ValueError("Immutable registry history changed")
                    for id, assumption in previous.assumptions.items():
                        current = graph.assumptions.get(id)
                        if current is None or current.rationale != assumption.rationale or current.evidence_ids != assumption.evidence_ids or current.history[:len(assumption.history)] != assumption.history:
                            raise ValueError("Original assumption or history changed")
                    if graph.boundary != previous.boundary and graph.boundary.revision <= previous.boundary.revision:
                        raise ValueError("Boundary changed without an explicit revision")
                    if graph.boundary.revision < previous.boundary.revision or graph.boundary.history[:len(previous.boundary.history)] != previous.boundary.history:
                        raise ValueError("Boundary history changed")
                versions.append(graph.to_dict())
                previous = graph
            _lineage(versions)
            normalized["versions"] = versions
            if envelope.get("graph") is None:
                if versions or normalized["execution_state"] is not None:
                    raise ValueError("Uninitialized state contains graph history or execution state")
            else:
                current = Graph.from_dict(envelope["graph"])
                if not versions or current.to_dict() != versions[-1]:
                    raise ValueError("Current graph does not match committed history")
                normalized["graph"] = current.to_dict()
                if normalized["execution_state"] is not None:
                    state = decode_record(ExecutionState, normalized["execution_state"])
                    # Execution checkpoints may lag after a graph mutation, but
                    # remain valid against the exact historical graph they reference.
                    if not 0 <= state.graph_version <= current.version:
                        raise ValueError("Execution state references an unavailable version")
                    target = Graph.from_dict(versions[state.graph_version])
                    if _execution_findings(state, target):
                        raise ValueError("Invalid execution state")
                    normalized["execution_state"] = asdict(state)
            call_ids = set()
            for index, event in enumerate(normalized["trace"], 1):
                if not isinstance(event, dict) or type(event.get("sequence")) is not int or event.get("sequence") != index:
                    raise ValueError("Invalid trace ordering")
                if legacy:
                    event.setdefault("tool_call_id", "legacy-" + str(index))
                    event.setdefault("outcomes", [])
                    event.setdefault("errors", event.get("findings", []))
                call_id = event.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id.strip() or call_id in call_ids:
                    raise ValueError("Invalid tool call identity")
                if not isinstance(event.get("tool"), str) or not isinstance(event.get("arguments"), dict) or not isinstance(event.get("result"), str):
                    raise ValueError("Invalid trace event")
                version = event.get("graph_version")
                if version is not None and (type(version) is not int or not 0 <= version < len(versions)):
                    raise ValueError("Trace references an unavailable version")
                if any(not isinstance(event.get(key), list) for key in ("findings", "errors", "outcomes")):
                    raise ValueError("Invalid trace findings or outcomes")
                if datetime.fromisoformat(event["timestamp"]).tzinfo is None:
                    raise ValueError("Trace timestamps must identify their timezone")
                for finding in [*event["findings"], *event["errors"]]:
                    decode_record(Finding, finding)
                if any(not isinstance(outcome, dict) or outcome.get("result") not in ("committed", "queued", "restored", "rejected", "rolled_back", "not_attempted") for outcome in event["outcomes"]):
                    raise ValueError("Invalid operation outcomes")
                call_ids.add(call_id)
            if contains_secret(normalized):
                raise ValueError("Sensitive values in persisted state")
            return normalized
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            raise CorruptState("Persisted state failed integrity or schema validation; inspect the separate snapshot") from exc

    def _read_valid(self, path: Path) -> dict:
        try:
            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle, object_pairs_hook=unique_keys)
            return self._validate_envelope(value)
        except (ValueError, TypeError) as exc:
            raise CorruptState("State is not valid JSON") from exc

    def _load(self) -> dict:
        return self._read_valid(self.path)

    @staticmethod
    def _atomic_write(path: Path, envelope: dict) -> None:
        payload = deepcopy(envelope)
        payload.pop("checksum", None)
        payload["checksum"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        data = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write(self, envelope: dict) -> None:
        # Validate the full candidate envelope before either filesystem write.
        check = deepcopy(envelope)
        check["checksum"] = hashlib.sha256(canonical_json(check).encode()).hexdigest()
        self._validate_envelope(check)
        previous = self._load() if self.path.exists() else envelope
        if previous["graph"] is None and envelope["graph"] is not None:
            previous = envelope
        self._atomic_write(self.snapshot_path, previous)
        self._atomic_write(self.path, envelope)

    @staticmethod
    def _event(envelope: dict, tool: str, arguments: dict, result: str,
               findings: list[Finding], call_id: str, outcomes: list[dict] | None = None) -> None:
        envelope["trace"].append(redact({
            "sequence": len(envelope["trace"]) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_call_id": call_id, "tool": tool, "arguments": arguments, "result": result,
            "graph_version": envelope["graph"]["version"] if envelope["graph"] else None,
            "findings": [asdict(f) for f in findings],
            "errors": [asdict(f) for f in findings] if result not in ("ok", "valid") else [],
            "outcomes": outcomes or [],
        }))

    def _failure(self, tool: str, args: dict, call_id: str, kind: str) -> None:
        # Best-effort diagnostic sidecar when primary state cannot accept an event.
        event = redact({"timestamp": datetime.now(timezone.utc).isoformat(), "tool_call_id": call_id,
                        "tool": tool, "arguments": args, "result": "storage_error",
                        "errors": [{"code": kind, "message": "Primary transaction unavailable"}]})
        try:
            self.failure_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.failure_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(canonical_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass  # No audit system can guarantee writes on an unavailable disk.

    @contextmanager
    def _session(self, tool: str, args: dict, call_id: str | None, *, initialize: bool = False):
        valid_id = isinstance(call_id, str) and bool(call_id.strip()) and not contains_secret(call_id)
        id = call_id if valid_id else str(uuid4())
        try:
            with self._locked():
                envelope = self._empty() if initialize and not self.path.exists() else self._load()
                if call_id is not None and not valid_id:
                    args = {**args, "requested_tool_call_id": redact(call_id)}
                    self._reject(envelope, tool, args, id, [Finding("invalid_call_id", "tool", "Tool call ID must be a nonempty nonsensitive string")])
                if any(e["tool_call_id"] == id for e in envelope["trace"]):
                    args = {**args, "duplicate_tool_call_id": id}
                    findings = [Finding("duplicate_call_id", "tool", "Tool call IDs must be unique; inspect the original outcome")]
                    self._event(envelope, tool, args, "rejected", findings, str(uuid4()))
                    self._write(envelope)
                    raise MutationRejected(findings)
                yield envelope, id
        except (StoreBusy, CorruptState, OSError) as exc:
            self._failure(tool, args, id, type(exc).__name__)
            raise

    @staticmethod
    def _rejected_outcomes(args: dict) -> list[dict]:
        operations = args.get("operations")
        if not isinstance(operations, list):
            return []
        return [{"index": index, "target_id": item.get("id") if isinstance(item, dict) else None,
                 "result": "rejected"} for index, item in enumerate(operations)]

    def _reject(self, envelope: dict, tool: str, args: dict, id: str,
                findings: list[Finding], outcomes: list[dict] | None = None):
        self._event(envelope, tool, args, "rejected", findings, id, outcomes if outcomes else self._rejected_outcomes(args))
        self._write(envelope)
        raise MutationRejected(findings)

    def _require_graph(self, envelope: dict, tool: str, args: dict, id: str) -> Graph:
        if envelope["graph"] is None:
            self._reject(envelope, tool, args, id, [Finding("uninitialized", "graph", "No initialized graph")])
        return Graph.from_dict(envelope["graph"])

    def _require_version(self, envelope: dict, tool: str, args: dict, id: str, expected: int):
        if type(expected) is not int or expected != envelope["graph"]["version"]:
            findings = [Finding("version_conflict", "graph", "Expected version does not match current version")]
            self._event(envelope, tool, args, "version_conflict", findings, id, self._rejected_outcomes(args))
            self._write(envelope)
            raise VersionConflict("Expected version does not match current version")

    def initialize(self, graph: Graph, *, tool_call_id: str | None = None) -> Graph:
        args = {"graph": redact(graph)}
        with self._session("initialize", args, tool_call_id, initialize=True) as (envelope, id):
            if envelope["graph"] is not None:
                self._event(envelope, "initialize", args, "rejected", [Finding("already_initialized", "graph", "Initialization cannot replace an existing store")], id)
                self._write(envelope)
                raise StoreError("Store already exists; initialization never overwrites its graph")
            findings = validate(graph)
            if isinstance(graph, Graph) and graph.version != 0:
                findings.append(Finding("initial_version", graph.id, "New graph must start at version zero"))
            if contains_secret(graph):
                findings.append(Finding("sensitive_payload", "graph", "Sensitive content is prohibited in persisted graphs"))
            if findings:
                self._reject(envelope, "initialize", args, id, findings)
            envelope["graph"] = graph.to_dict()
            envelope["versions"].append(graph.to_dict())
            self._event(envelope, "initialize", args, "ok", [], id)
            self._write(envelope)
            return deepcopy(graph)

    def get_process_graph(self, *, tool_call_id: str | None = None) -> Graph:
        with self._session("get_process_graph", {}, tool_call_id) as (envelope, id):
            graph = self._require_graph(envelope, "get_process_graph", {}, id)
            self._event(envelope, "get_process_graph", {}, "ok", [], id)
            self._write(envelope)
            return graph

    def validate_process_graph(self, *, tool_call_id: str | None = None) -> list[Finding]:
        with self._session("validate_process_graph", {}, tool_call_id) as (envelope, id):
            graph = self._require_graph(envelope, "validate_process_graph", {}, id)
            findings = validate(graph)
            self._event(envelope, "validate_process_graph", {}, "invalid" if findings else "valid", findings, id)
            self._write(envelope)
            return findings

    def update_process_graph(self, operations: list[Operation], expected_version: int, *,
                             origin: str = "manual", assessments: list[BoundaryAssessment] | None = None,
                             tool_call_id: str | None = None) -> Graph:
        tool = "update_process_graph"
        proposed = redact(operations)
        if isinstance(proposed, list):
            for item in proposed:
                if isinstance(item, dict):
                    item["payload_sha256"] = safe_digest({k: v for k, v in item.items() if k != "payload_sha256"})
        args = {"expected_version": redact(expected_version), "origin": redact(origin),
                "operations": proposed, "assessments": redact(assessments or [])}
        with self._session(tool, args, tool_call_id) as (envelope, id):
            graph = self._require_graph(envelope, tool, args, id)
            self._require_version(envelope, tool, args, id, expected_version)
            findings, outcomes = [], []
            if origin not in ("manual", "agent") or not isinstance(operations, list) or not operations:
                findings.append(Finding("operations", "graph", "Origin and nonempty typed operation list are required"))
            if assessments is not None and not isinstance(assessments, list):
                findings.append(Finding("boundary_assessment", "graph", "Assessments must be a list"))
            candidate = deepcopy(graph)
            lineage = _lineage(envelope["versions"])
            used_assessments = set()
            if not findings:
                if origin == "agent" and any(op.action in ("confirm_boundary", "revise_boundary") for op in operations if isinstance(op, Operation)) and len(operations) > 1:
                    findings.append(Finding("boundary_batch", "graph", "Agent boundary changes must be standalone transactions"))
            if not findings:
                for index, op in enumerate(operations):
                    outcome = {"index": index, "target_id": redact(op.id) if isinstance(op, Operation) else None,
                               "collection": redact(op.collection) if isinstance(op, Operation) else None,
                               "action": redact(op.action) if isinstance(op, Operation) else None, "result": "rejected"}
                    try:
                        if not matches_schema(op, Operation):
                            raise PolicyViolation("Operation must be a valid typed record")
                        if contains_secret(op):
                            raise PolicyViolation("Sensitive content is prohibited in graph proposals")
                        matching = [a for a in assessments or [] if matches_schema(a, BoundaryAssessment) and a.proposal_digest == proposal_digest(op)]
                        if origin == "agent" and len(matching) != 1 or len(matching) > 1:
                            raise PolicyViolation("Exactly one completed assessment per agent operation is required")
                        if matching:
                            check_assessment(candidate, op, matching[0])
                            used_assessments.add(matching[0].id)
                        apply_operation(candidate, op, origin=origin, lineage=lineage)
                        outcome["result"] = "prepared"
                    except (PolicyViolation, TypeError, ValueError, KeyError, AttributeError, RecursionError) as exc:
                        message = str(exc) if isinstance(exc, PolicyViolation) else "Malformed operation payload"
                        outcome["error"] = {"code": "operation_policy", "message": message}
                        findings.append(Finding("operation_policy", op.id if isinstance(op, Operation) and isinstance(op.id, str) else "graph", message, "policy"))
                    outcomes.append(outcome)
                    if findings:
                        for next_index in range(index + 1, len(operations)):
                            remaining = operations[next_index]
                            outcomes.append({"index": next_index, "target_id": redact(remaining.id) if isinstance(remaining, Operation) else None, "result": "not_attempted"})
                        break
                if not findings and assessments and (any(not matches_schema(a, BoundaryAssessment) for a in assessments) or len(used_assessments) != len(assessments)):
                    findings.append(Finding("boundary_assessment", "graph", "Unmatched, duplicate or invalid assessments"))
            if not findings:
                candidate.version += 1
                findings = validate(candidate)
                if contains_secret(candidate):
                    findings.append(Finding("sensitive_payload", "graph", "Sensitive content is prohibited"))
            if findings:
                for outcome in outcomes:
                    if outcome["result"] == "prepared":
                        outcome["result"] = "rolled_back"
                self._reject(envelope, tool, args, id, findings, outcomes)
            envelope["graph"] = candidate.to_dict()
            envelope["versions"].append(candidate.to_dict())
            for outcome in outcomes:
                outcome["result"] = "committed"
            self._event(envelope, tool, args, "ok", [], id, outcomes)
            self._write(envelope)
            return deepcopy(candidate)

    def get_execution_state(self, *, tool_call_id: str | None = None) -> ExecutionState | None:
        with self._session("get_execution_state", {}, tool_call_id) as (envelope, id):
            state = decode_record(ExecutionState, envelope["execution_state"]) if envelope["execution_state"] else None
            self._event(envelope, "get_execution_state", {}, "ok", [], id)
            self._write(envelope)
            return state

    def save_execution_state(self, state: ExecutionState, expected_state_version: int,
                             expected_graph_version: int, *, tool_call_id: str | None = None) -> ExecutionState:
        tool = "save_execution_state"
        args = {"state": redact(state), "expected_state_version": redact(expected_state_version),
                "expected_graph_version": redact(expected_graph_version)}
        with self._session(tool, args, tool_call_id) as (envelope, id):
            graph = self._require_graph(envelope, tool, args, id)
            self._require_version(envelope, tool, args, id, expected_graph_version)
            current_version = envelope["execution_state"]["version"] if envelope["execution_state"] else -1
            if type(expected_state_version) is not int or expected_state_version != current_version:
                self._event(envelope, tool, args, "version_conflict", [Finding("execution_conflict", "run", "Execution state version conflict")], id)
                self._write(envelope)
                raise VersionConflict("Execution state version conflict")
            findings = _execution_findings(state, graph)
            if matches_schema(state, ExecutionState) and state.version != current_version + 1:
                findings.append(Finding("execution_version", state.run_id, "Execution state version must increment once"))
            if envelope["execution_state"] and matches_schema(state, ExecutionState) and state.run_id != envelope["execution_state"]["run_id"]:
                findings.append(Finding("run_identity", state.run_id, "Run identity cannot change"))
            if contains_secret(state):
                findings.append(Finding("sensitive_payload", "run", "Sensitive execution state is prohibited"))
            if findings:
                self._reject(envelope, tool, args, id, findings)
            envelope["execution_state"] = asdict(state)
            self._event(envelope, tool, args, "ok", [], id)
            self._write(envelope)
            return deepcopy(state)

    def propose_question(self, question: Question, assessment: BoundaryAssessment,
                         expected_version: int, *, tool_call_id: str | None = None) -> Question:
        tool = "propose_question"
        args = {"question": redact(question), "assessment": redact(assessment), "expected_version": redact(expected_version)}
        with self._session(tool, args, tool_call_id) as (envelope, id):
            graph = self._require_graph(envelope, tool, args, id)
            self._require_version(envelope, tool, args, id, expected_version)
            try:
                if contains_secret(question) or contains_secret(assessment):
                    raise PolicyViolation("Sensitive question content is prohibited")
                check_question(graph, question, assessment)
                if envelope["execution_state"] is None:
                    raise PolicyViolation("Create execution state before proposing questions")
                state = decode_record(ExecutionState, envelope["execution_state"])
                if state.graph_version != graph.version:
                    raise PolicyViolation("Refresh execution state after graph changes")
                if any(p.question.id == question.id for p in state.pending_questions):
                    raise PolicyViolation("Pending question identity already exists")
            except (PolicyViolation, TypeError, ValueError) as exc:
                message = str(exc) if isinstance(exc, PolicyViolation) else "Malformed question"
                self._reject(envelope, tool, args, id, [Finding("question_boundary", "question", message, "policy")])
            state.pending_questions.append(PendingQuestion(deepcopy(question), deepcopy(assessment), graph.version))
            state.version += 1
            envelope["execution_state"] = asdict(state)
            self._event(envelope, tool, args, "ok", [], id, [{"target_id": question.id, "result": "queued"}])
            self._write(envelope)
            return deepcopy(question)

    def recover_from_snapshot(self, *, tool_call_id: str | None = None) -> Graph:
        """Offline explicit recovery. Never bypasses an active or stale lock."""
        call_id = tool_call_id if isinstance(tool_call_id, str) and tool_call_id.strip() and not contains_secret(tool_call_id) else str(uuid4())
        args = {"snapshot": self.snapshot_path.name}
        try:
            with self._locked():
                envelope = self._read_valid(self.snapshot_path)
                if envelope["graph"] is None:
                    raise CorruptState("Snapshot has no initialized graph")
                if any(e["tool_call_id"] == call_id for e in envelope["trace"]):
                    raise StoreError("Recovery tool call ID already exists")
                if envelope["execution_state"] is not None:
                    envelope["execution_state"]["status"] = "paused"
                    envelope["execution_state"]["version"] += 1
                self._event(envelope, "recover_from_snapshot", args, "ok", [], call_id,
                            [{"target_id": envelope["graph"]["id"], "result": "restored", "version": envelope["graph"]["version"]}])
                self._atomic_write(self.path, envelope)
                return Graph.from_dict(envelope["graph"])
        except (StoreBusy, CorruptState, OSError) as exc:
            self._failure("recover_from_snapshot", args, call_id, type(exc).__name__)
            raise

    def agent_tools(self) -> "AgentGraphTools":
        """Expose this adapter, rather than origin or host-only APIs, to an agent."""
        return AgentGraphTools(self)

    def inspect_trace(self) -> list[dict]:
        """Diagnostic read; graph-agent calls are persisted by their methods."""
        with self._locked():
            return deepcopy(self._load()["trace"])


class AgentGraphTools:
    """Fixed agent origin; assessments must be supplied by the trusted host reviewer.

    This is a tool adapter, not an orchestration engine or semantic reviewer.
    Initialization, confirmation recording, recovery and execution-state writes
    remain host-only APIs.
    """
    def __init__(self, store: GraphStore):
        self._store = store

    def get_process_graph(self, *, tool_call_id: str | None = None) -> Graph:
        return self._store.get_process_graph(tool_call_id=tool_call_id)

    def validate_process_graph(self, *, tool_call_id: str | None = None) -> list[Finding]:
        return self._store.validate_process_graph(tool_call_id=tool_call_id)

    def update_process_graph(self, operations: list[Operation], expected_version: int, *,
                             assessments: list[BoundaryAssessment] | None = None,
                             tool_call_id: str | None = None) -> Graph:
        return self._store.update_process_graph(operations, expected_version, origin="agent",
                                                assessments=assessments, tool_call_id=tool_call_id)

    def propose_question(self, question: Question, assessment: BoundaryAssessment,
                         expected_version: int, *, tool_call_id: str | None = None) -> Question:
        return self._store.propose_question(question, assessment, expected_version, tool_call_id=tool_call_id)
