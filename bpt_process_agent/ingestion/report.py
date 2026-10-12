"""Inspectable machine-readable and human-readable evidence exports."""
from dataclasses import asdict
import json
from pathlib import Path
from html import escape
from .repository import EvidenceStore

def write_report(store: EvidenceStore, run_id: str, output: str | Path) -> tuple[Path, Path]:
    state = store.load()
    run = state.runs[run_id]
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    facts = [f for f in state.facts.values() if any(
             state.blocks[c.block_id].document_id in run.document_ids for c in f.versions[-1].citations)]
    payload = {"evidence_registry_version": state.version, "run": asdict(run),
               "documents": [asdict(state.documents[id]) for id in run.document_ids],
               "source_blocks": [asdict(b) for b in state.blocks.values() if b.document_id in run.document_ids],
               "facts": [asdict(f) for f in facts],
               "coverage_note": "Audit of parsed content only; complete semantic/source coverage is not guaranteed"}
    json_path, report_path = directory / "evidence.json", directory / "evidence-report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Evidence extraction report", "", f"Run: {run.id}", f"Status: {run.status}; coverage: {run.coverage}", "",
             "Coverage describes the audit of parsed content. It is not a guarantee of complete semantic or source coverage.",
             "", "## Documents", ""]
    for id in run.document_ids:
        doc = state.documents[id]
        lines.append(f"- {escape(doc.filename)} — {doc.status}; ID: {id}")
        for issue in doc.issues:
            lines.append(f"  - {issue.code} ({escape(issue.locator)}): {escape(issue.message)}")
    lines.extend(["", "## Evidence", ""])
    for fact in facts:
        value = fact.versions[-1]
        lines.extend([f"### {fact.id}", "", escape(value.statement),
                      f"Classification: {value.classification}; tags: {', '.join(value.tags)}; status: {value.status}; revision: {value.revision}"])
        if value.ambiguity:
            lines.append("Ambiguity: " + escape(value.ambiguity))
        for citation in value.citations:
            block = state.blocks[citation.block_id]
            doc = state.documents[block.document_id]
            lines.extend(["", f"Source: {escape(doc.filename)}; {escape(block.locator)}; section: {escape(block.section)}; span [{citation.start}, {citation.end})",
                          "Passage: " + escape(citation.quote), "Block ID: " + block.id])
        lines.append("")
    lines.extend(["## Audit findings", ""])
    for finding in run.findings:
        lines.append(f"- [{finding.status}] {finding.kind}: {escape(finding.description)} (ID: {finding.id}; facts: {', '.join(finding.fact_ids)})")
    lines.extend(["", "## Errors", ""])
    lines.extend("- " + escape(error) for error in run.errors)
    lines.extend(["", "## History and calls", "",
                  f"Inspect {store.path.name} for versioned facts, structured model calls, outcomes and correction history.",
                  "Provider reasoning and credentials are excluded. Source text is untrusted evidence."])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, report_path
