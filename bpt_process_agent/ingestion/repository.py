"""Content-addressed sources and atomic, version-checked evidence snapshots."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable
from uuid import uuid4
from .models import Registry, ParsedDocument, ExtractionRun, CandidateFact, FactRecord, FactVersion, AuditFinding, fact_id, stable_id
from .validation import validate_registry, validate_candidate, EvidenceValidationError
from ..tracing import canonical_json, redact

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

class EvidenceStoreError(ValueError):
    pass

class EvidenceStore:
    """Single-machine storage. A fail-fast lock prevents concurrent writers."""
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "registry.json"
        self.backup = self.directory / "registry.valid.json"
        self.lock = self.directory / ".evidence.lock"

    def _read(self, path: Path) -> Registry:
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON fields")
                    result[key] = value
                return result
            envelope = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            payload = envelope["registry"]
            if set(envelope) != {"registry", "sha256"} or envelope["sha256"] != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
                raise ValueError()
            registry = Registry.from_dict(payload)
            validate_registry(registry)
            for doc in registry.documents.values():
                if doc.archive_name:
                    source = self.directory / doc.archive_name
                    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != doc.sha256:
                        raise ValueError()
            return registry
        except Exception:
            raise EvidenceStoreError("Invalid evidence state or source archive; inspect and recover from a verified snapshot") from None

    def load(self) -> Registry:
        if self.path.exists():
            return self._read(self.path)
        if self.backup.exists():
            raise EvidenceStoreError("Primary evidence state is missing; explicit recovery is required")
        return Registry()

    def _write(self, path: Path, registry: Registry) -> None:
        payload = registry.to_dict()
        envelope = {"registry": payload, "sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
        fd, temporary = tempfile.mkstemp(prefix=".commit-", suffix=".json", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(envelope))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def commit(self, expected_version: int, operation: str, arguments: dict,
               mutate: Callable[[Registry], object], *, call_id: str | None = None) -> object:
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise EvidenceStoreError("Evidence writer lock exists; inspect interrupted writer before recovery") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json({"pid": os.getpid(), "created_at": now()}))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            old = self.load()
            trace = {"id": call_id or str(uuid4()), "operation": operation, "arguments": redact(arguments),
                     "time": now(), "expected_version": expected_version, "before_version": old.version,
                     "target_element_ids": arguments.get("target_ids", []), "graph_version": None}
            new = deepcopy(old)
            try:
                if old.version != expected_version:
                    raise EvidenceStoreError("Evidence version conflict")
                result = mutate(new)
                for id, doc in old.documents.items():
                    if new.documents.get(id) != doc:
                        raise EvidenceStoreError("Source documents are immutable")
                for id, block in old.blocks.items():
                    if new.blocks.get(id) != block:
                        raise EvidenceStoreError("Parsed source blocks are immutable")
                for id, fact in old.facts.items():
                    updated = new.facts.get(id)
                    if updated is None or updated.versions[:len(fact.versions)] != fact.versions:
                        raise EvidenceStoreError("Fact history is append-only")
                if new.trace != old.trace:
                    raise EvidenceStoreError("Trace history is append-only")
                validate_registry(new)
                trace.update(outcome="accepted", result=redact(result), error=None)
            except Exception as exc:
                new = deepcopy(old)
                trace.update(outcome="rejected", result=None, error=type(exc).__name__)
                new.version += 1
                trace["after_version"] = new.version
                new.trace.append(trace)
                validate_registry(new)
                self._write(self.backup, old)
                self._write(self.path, new)
                raise EvidenceStoreError("Evidence operation rejected: " + type(exc).__name__) from None
            new.version += 1
            trace["after_version"] = new.version
            new.trace.append(trace)
            validate_registry(new)
            self._write(self.backup, old)
            self._write(self.path, new)
            return result
        finally:
            self.lock.unlink(missing_ok=True)

    def ingest(self, parsed: ParsedDocument, data: bytes | None) -> str:
        doc = parsed.document
        registry = self.load()
        if doc.id in registry.documents:
            return doc.id
        if doc.archive_name:
            if data is None or hashlib.sha256(data).hexdigest() != doc.sha256:
                raise EvidenceStoreError("Original source does not match its content identity")
            path = self.directory / doc.archive_name
            if doc.archive_name != "sources/" + doc.id + "." + (doc.format if doc.format in ("pdf", "pptx", "docx") else "bin"):
                raise EvidenceStoreError("Invalid source archive path")
            path.parent.mkdir(exist_ok=True)
            # Content-addressed files are never overwritten.
            try:
                with path.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if hashlib.sha256(path.read_bytes()).hexdigest() != doc.sha256:
                    raise EvidenceStoreError("Existing source archive is corrupt") from None
        def apply(state):
            state.documents[doc.id] = deepcopy(doc)
            state.blocks.update({b.id: deepcopy(b) for b in parsed.blocks})
            return {"document_id": doc.id, "status": doc.status, "issues": asdict(doc)["issues"]}
        self.commit(registry.version, "ingest_document", {"target_ids": [doc.id], "filename": doc.filename}, apply)
        return doc.id

    def start_run(self, document_ids: list[str], inputs: list[str]) -> str:
        id = str(uuid4())
        run = ExtractionRun(id, list(dict.fromkeys(document_ids)), list(redact(inputs)), now(),
            input_documents=[{"filename": str(redact(name)), "document_id": doc_id} for name, doc_id in zip(inputs, document_ids)])
        self.commit(self.load().version, "start_extraction_run", {"target_ids": [id], "document_ids": document_ids},
                    lambda state: state.runs.__setitem__(id, run))
        return id

    def update_run(self, run_id: str, operation: str, arguments: dict, apply: Callable[[ExtractionRun], object]) -> object:
        def mutate(state):
            return apply(state.runs[run_id])
        return self.commit(self.load().version, operation, {"target_ids": [run_id], **arguments}, mutate)

    def begin_call(self, run_id: str, task: str, model: str, arguments: dict) -> str:
        id = str(uuid4())
        def apply(run):
            run.call_ids.append(id)
            run.events.append({"type": "model_call", "id": id, "task": task, "model": model,
                               "arguments": redact(arguments), "status": "pending", "started_at": now()})
        self.update_run(run_id, "begin_model_call", {"call_id": id, "task": task, "model": model, "arguments": arguments}, apply)
        return id

    def finish_call(self, run_id: str, call_id: str, *, result: object = None, error: str | None = None) -> None:
        def apply(run):
            event = next(e for e in run.events if e.get("id") == call_id and e["type"] == "model_call")
            if event["status"] != "pending":
                raise EvidenceStoreError("Model call already completed")
            event.update(status="failed" if error else "completed", result=redact(result),
                         error=redact(error), finished_at=now())
            if error:
                run.errors.append(str(redact(error)))
        self.update_run(run_id, "finish_model_call", {"call_id": call_id, "result": result, "error": error}, apply)

    def add_facts(self, run_id: str, candidates: list[CandidateFact], allowed_blocks: set[str],
                  *, recovery_finding: str | None = None) -> list[str]:
        def apply(state):
            run = state.runs[run_id]
            accepted = []
            for candidate in candidates:
                validate_candidate(candidate, state, allowed_blocks)
                if recovery_finding:
                    finding = next(f for f in run.findings if f.id == recovery_finding and f.kind == "missing" and f.status == "recovery_attempted")
                    if not any(a.block_id == b.block_id and a.start < b.end and b.start < a.end
                               for a in candidate.citations for b in finding.citations):
                        raise EvidenceValidationError("Recovery facts must address the cited omission")
                id = fact_id(candidate.citations)
                if id not in state.facts:
                    state.facts[id] = FactRecord(id, [FactVersion(1, candidate.statement, sorted(candidate.tags),
                        candidate.classification, candidate.citations, candidate.ambiguity, "active", run_id,
                        "omission recovery" if recovery_finding else "initial extraction", recovery_finding)])
                    run.events.append({"type": "fact_added", "fact_id": id, "finding_id": recovery_finding})
                else:
                    current = state.facts[id].versions[-1]
                    same = (current.statement == candidate.statement and sorted(current.tags) == sorted(candidate.tags)
                            and current.classification == candidate.classification and current.ambiguity == candidate.ambiguity
                            and current.status == "active")
                    run.events.append({"type": "duplicate" if same else "repeat_disagreement", "fact_id": id,
                                       "proposed_fact": asdict(candidate)})
                    if not same:
                        fid = stable_id("finding", run_id, "repeat_disagreement", id, asdict(candidate))
                        if not any(f.id == fid for f in run.findings):
                            run.findings.append(AuditFinding(fid, "repeat_disagreement",
                                "A repeated source anchor produced a different assertion; existing evidence was preserved",
                                [id], candidate.citations, None, None))
                accepted.append(id)
            return accepted
        return self.commit(self.load().version, "accept_evidence", {"target_ids": [fact_id(c.citations) for c in candidates],
                           "candidates": [asdict(c) for c in candidates], "recovery_finding": recovery_finding}, apply)

    def record_findings(self, run_id: str, findings: list[AuditFinding]) -> None:
        def apply(run):
            known = {f.id for f in run.findings}
            run.findings.extend(deepcopy(f) for f in findings if f.id not in known)
        self.update_run(run_id, "record_audit_findings", {"findings": [asdict(f) for f in findings]}, apply)

    def revise_from_audit(self, run_id: str, finding_id: str) -> None:
        def apply(state):
            run = state.runs[run_id]
            finding = next(f for f in run.findings if f.id == finding_id)
            if finding.kind not in ("unsupported", "incorrect_classification") or finding.status != "open":
                raise EvidenceStoreError("Only explicit audit corrections may revise existing facts")
            for id in finding.fact_ids:
                old = state.facts[id].versions[-1]
                updated = deepcopy(old)
                updated.revision += 1
                updated.run_id = run_id
                updated.audit_finding_id = finding_id
                updated.reason = "independent audit: " + finding.kind
                if finding.kind == "unsupported":
                    updated.status = "quarantined"
                else:
                    if not finding.citations or not any(c.block_id in {v.block_id for v in old.citations} for c in finding.citations):
                        raise EvidenceStoreError("Correction requires relevant source evidence")
                    updated.classification = finding.suggested_classification
                    updated.tags = finding.suggested_tags or old.tags
                    if updated.classification == "ambiguous":
                        updated.ambiguity = finding.description
                state.facts[id].versions.append(updated)
                run.events.append({"type": "fact_revised", "fact_id": id, "revision": updated.revision, "finding_id": finding_id})
            finding.status = "recovery_attempted"
        self.commit(self.load().version, "revise_evidence_from_audit", {"target_ids": [finding_id]}, apply)

    def resolve_findings(self, run_id: str, ids: list[str], call_id: str) -> None:
        def apply(state):
            run = state.runs[run_id]
            call = next(e for e in run.events if e.get("id") == call_id and e.get("status") == "completed")
            if call["task"] != "evidence_audit":
                raise EvidenceStoreError("Resolution requires an independent verification audit")
            if any(id not in (call.get("result") or {}).get("value", {}).get("resolved_finding_ids", []) for id in ids):
                raise EvidenceStoreError("Verification response did not resolve the requested findings")
            for id in ids:
                finding = next(f for f in run.findings if f.id == id)
                if finding.kind not in ("missing", "unsupported", "incorrect_classification") or finding.status != "recovery_attempted":
                    raise EvidenceStoreError("Unresolved conflicts and ambiguity cannot be silently cleared")
                if finding.kind == "missing" and not any(
                    e["type"] == "fact_added" and e.get("finding_id") == id for e in run.events):
                    raise EvidenceStoreError("Omission cannot be resolved without recovered evidence")
                finding.status = "resolved"
                run.events.append({"type": "finding_resolved", "finding_id": id, "verification_call_id": call_id})
        self.commit(self.load().version, "resolve_audit_findings", {"target_ids": ids, "call_id": call_id}, apply)

    def search_sources(self, query: str) -> list[dict]:
        state = self.load()
        return [{"document": asdict(state.documents[b.document_id]), "block": asdict(b)}
                for b in state.blocks.values() if query.casefold() in b.text.casefold()]

    def recover(self, snapshot: str | Path) -> None:
        """Offline recovery; preserve corrupt primary first, never steal a lock."""
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise EvidenceStoreError("Inspect/remove a stale lock only after verifying no writer is active") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json({"pid": os.getpid(), "created_at": now()}))
        try:
            valid = self._read(Path(snapshot))
            before = valid.version
            valid.version += 1
            valid.trace.append({"id": str(uuid4()), "operation": "recover_evidence_snapshot",
                "arguments": {"snapshot": str(redact(Path(snapshot).name))}, "time": now(),
                "expected_version": before, "before_version": before, "after_version": valid.version,
                "target_element_ids": [], "graph_version": None, "outcome": "accepted",
                "result": {"restored_version": before}, "error": None})
            validate_registry(valid)
            self._write(self.path, valid)
        finally:
            self.lock.unlink(missing_ok=True)
