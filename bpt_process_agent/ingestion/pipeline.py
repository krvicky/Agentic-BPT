"""Bounded evidence-only pipeline; no reconstruction or orchestration engine."""
from dataclasses import asdict
from pathlib import Path
from typing import Callable, TypeVar
from .llm import StructuredClient
from ..llm_config import LLMConfig
from .models import AuditFinding, Citation, stable_id
from .parsers import parse_document
from .prompts import batches, extraction_messages, audit_messages
from .repository import EvidenceStore, now
from .validation import EXTRACTION_SCHEMA, AUDIT_SCHEMA, extraction_candidates, audit_findings, EvidenceValidationError, validate_json

T = TypeVar("T")

class ExtractionPipeline:
    def __init__(self, store: EvidenceStore, client: StructuredClient | None, config: LLMConfig | None,
                 *, extraction_model: str | None = None, audit_model: str | None = None,
                 batch_chars: int = 16000, max_calls: int = 100):
        if batch_chars < 100:
            raise ValueError("Source batch limit must be at least 100 characters")
        if max_calls < 1:
            raise ValueError("Model call budget must be positive")
        self.store, self.client, self.config = store, client, config
        self.extraction_model, self.audit_model = extraction_model, audit_model
        self.batch_chars, self.max_calls = batch_chars, max_calls
        self.calls = 0

    def _call(self, run_id: str, task: str, model: str, arguments: dict, action: Callable[[], T]) -> tuple[T | None, str]:
        if self.calls >= self.max_calls:
            self.store.update_run(run_id, "model_budget_exhausted", {"task": task},
                                  lambda run: run.errors.append("Model call budget exhausted; remaining coverage is incomplete"))
            return None, ""
        self.calls += 1
        id = self.store.begin_call(run_id, task, model, arguments)
        try:
            result = action()
            if hasattr(result, "value"):
                validate_json(result.value, arguments["schema"])
            self.store.finish_call(run_id, id, result=asdict(result) if hasattr(result, "value") else result)
            return result, id
        except Exception as exc:
            # Never persist raw provider exceptions or hidden reasoning.
            self.store.finish_call(run_id, id, error="Model action failed: " + type(exc).__name__)
            return None, id

    def _invalid(self, run_id: str, call_id: str, description: str) -> None:
        finding = AuditFinding(stable_id("finding", run_id, call_id, description), "invalid_output",
                               description, [], [], None, None, call_id=call_id)
        self.store.record_findings(run_id, [finding])

    @staticmethod
    def _check_view_citations(citations: list[Citation], views: list[dict]) -> None:
        for c in citations:
            if not any(c.block_id == v["id"] and v["start"] <= c.start < c.end <= v["end"] for v in views):
                raise EvidenceValidationError("Citation extends outside supplied source views")

    def _extract(self, run_id: str, model: str, views: list[dict], missing: AuditFinding | None = None) -> list[str]:
        messages = extraction_messages(views, missing)
        result, call_id = self._call(run_id, "extraction", model,
            {"messages": messages, "schema": EXTRACTION_SCHEMA, "target_ids": [v["id"] for v in views]},
            lambda: self.client.complete(model, messages, EXTRACTION_SCHEMA, name="process_evidence"))
        if result is None:
            return []
        try:
            candidates = extraction_candidates(result.value, self.store.load(), {v["id"] for v in views})
            for candidate in candidates:
                self._check_view_citations(candidate.citations, views)
            return self.store.add_facts(run_id, candidates, {v["id"] for v in views},
                                        recovery_finding=missing.id if missing else None)
        except Exception:
            self._invalid(run_id, call_id, "Extraction output failed local citation/provenance validation; no facts from this response were accepted")
            return []

    def _audit(self, run_id: str, model: str, views: list[dict], previous: list[AuditFinding] | None = None) -> list[AuditFinding] | None:
        previous = previous or []
        state = self.store.load()
        allowed = {v["id"] for v in views}
        facts = [f for f in state.facts.values() if any(c.block_id in allowed for c in f.versions[-1].citations)]
        messages = audit_messages(views, facts, previous)
        result, call_id = self._call(run_id, "evidence_audit", model,
            {"messages": messages, "schema": AUDIT_SCHEMA, "target_ids": [f.id for f in facts]},
            lambda: self.client.complete(model, messages, AUDIT_SCHEMA, name="evidence_audit"))
        if result is None:
            return None
        try:
            findings, resolved = audit_findings(result.value, state, allowed, {f.id for f in facts}, run_id, call_id)
            for finding in findings:
                self._check_view_citations(finding.citations, views)
            if any(id not in {f.id for f in previous} for id in resolved):
                raise EvidenceValidationError("Audit tried to resolve a finding outside the verification set")
            self.store.record_findings(run_id, findings)
            if resolved:
                self.store.resolve_findings(run_id, resolved, call_id)
            return findings
        except Exception:
            self._invalid(run_id, call_id, "Audit output failed local finding/citation validation; proposed resolutions were rejected")
            return None

    def run(self, paths: list[str | Path], *, parse_only: bool = False) -> str:
        if not paths:
            raise ValueError("At least one document is required")
        self.calls = 0
        document_ids = []
        for path in paths:
            parsed, data = parse_document(path)
            document_ids.append(self.store.ingest(parsed, data))
        run_id = self.store.start_run(document_ids, [Path(p).name for p in paths])
        state = self.store.load()
        for id in set(document_ids):
            doc = state.documents[id]
            if doc.status != "parsed":
                findings = [AuditFinding(stable_id("finding", run_id, id, issue.code, issue.locator), "parsing_gap",
                           issue.message + " (" + doc.filename + ", " + issue.locator + ")", [], [], None, None)
                           for issue in doc.issues if issue.affects_coverage]
                self.store.record_findings(run_id, findings)
        blocks = [b for b in state.blocks.values() if b.document_id in document_ids]
        views_batches = batches(blocks, self.batch_chars)
        audited = 0
        if not parse_only and blocks:
            if self.config is None or self.client is None:
                self.store.update_run(run_id, "configuration_failure", {}, lambda r: r.errors.append("LLM configuration/client is required"))
            else:
                extract = self.config.model_for("extraction", override=self.extraction_model)
                audit = self.config.model_for("evidence_audit", override=self.audit_model)
                if extract == audit:
                    self.store.update_run(run_id, "configuration_failure", {},
                        lambda r: r.errors.append("Independent audit requires a different effective model from extraction"))
                else:
                    ecap, _ = self._call(run_id, "capability_check", extract, {"model": extract}, lambda: self.client.verify(extract))
                    acap, _ = self._call(run_id, "capability_check", audit, {"model": audit}, lambda: self.client.verify(audit))
                    if ecap is not None and acap is not None:
                        if len(views_batches) > 1:
                            self.store.record_findings(run_id, [AuditFinding(stable_id("finding", run_id, "cross_batch"), "parsing_gap",
                                "Source was split into bounded audit batches; cross-batch relationships/conflicts and split assertions need later review",
                                [], [], None, None)])
                        for views in views_batches:
                            self._extract(run_id, extract, views)
                            findings = self._audit(run_id, audit, views)
                            if findings is None:
                                continue
                            audited += 1
                            repaired = []
                            for finding in findings:
                                if finding.kind == "missing":
                                    self.store.update_run(run_id, "attempt_omission_recovery", {"finding_id": finding.id},
                                        lambda r, id=finding.id: setattr(next(f for f in r.findings if f.id == id), "status", "recovery_attempted"))
                                    recovered = self._extract(run_id, extract, views, missing=finding)
                                    if recovered:
                                        repaired.append(finding.id)
                                elif finding.kind in ("unsupported", "incorrect_classification"):
                                    try:
                                        self.store.revise_from_audit(run_id, finding.id)
                                        repaired.append(finding.id)
                                    except Exception:
                                        self._invalid(run_id, finding.call_id, "Proposed evidence correction was rejected; original history was preserved")
                            if repaired:
                                current = self.store.load().runs[run_id]
                                previous = [f for f in current.findings if f.id in repaired]
                                self._audit(run_id, audit, views, previous)
        def finish(run):
            state = self.store.load()
            clean = (not parse_only and bool(views_batches) and audited == len(views_batches)
                     and not run.errors and all(f.status == "resolved" for f in run.findings)
                     and all(state.documents[id].status == "parsed" for id in run.document_ids))
            run.finished_at = now()
            run.coverage = "audited_parsed_content" if clean else "not_audited" if parse_only else "incomplete"
            run.status = "audited" if clean else "partial" if blocks else "failed"
            run.events.append({"type": "run_finished", "audited_batches": audited, "source_batches": len(views_batches),
                               "coverage_note": "LLM audit result only; complete semantic coverage is not guaranteed"})
        self.store.update_run(run_id, "finish_extraction_run", {}, finish)
        return run_id
