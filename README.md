# BPT Process Agent — Milestones 1, 1.1 and 2

A standalone Python prototype with a hardened canonical graph core and a separate document/evidence extraction pipeline. Milestone 2 registers evidence without reconstructing graphs.

## Files and decisions

- bpt_process_agent/schemas.py: typed canonical records, public schema checking/decoding, assessment and resolution contracts, and separate execution-state records.
- bpt_process_agent/validation.py: deterministic schema, structure and provenance validation.
- bpt_process_agent/policy.py: proposal assessment gates, explicit lifecycle transitions and controlled alias lookup. BoundaryReviewer is a Protocol for future semantic review; there is no reviewer implementation.
- bpt_process_agent/storage.py: version-checked atomic mutations, fixed-origin agent tool adapter, trace persistence, execution-state persistence and recovery.
- bpt_process_agent/llm_config.py: environment-only Flash/Pro model routing and task overrides; transport lives in the separate ingestion package.
- bpt_process_agent/tracing.py: structured argument serialization and conservative credential redaction.
- tests/test_core.py and tests/test_storage.py: original core acceptance tests.
- tests/test_hardening.py and tests/test_transactions.py: architecture-hardening acceptance tests.
- tests/test_llm_config.py: environment loading, routing, overrides and credential-safe configuration tests.
- SPEC.md and AGENTS.md: authoritative requirements, synchronized with the workspace copies.

The Milestone 1/1.1 graph core uses the standard library. Milestone 2 adds the parser, SDK and schema dependencies documented below. The graph, version history and trace remain one local transaction envelope; execution state is a distinct record outside the canonical graph, with its own version.

## Run tests

Python 3.11+:

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[test]"
    .\.venv\Scripts\python.exe -m pytest -q

Use the existing workspace environment after installing the Milestone 2 dependencies.

Final Milestone 1.1 verification: **154 passed in 7.43s**, Python 3.11.15, pytest 9.1.1. Checkpoints passed at 79, 137, 149 and 150 tests before final redaction coverage. Tests cover original invariants plus assessment binding and rejection, boundary questions, batched lineage bypasses, resolution support/history/reopening, multiple pools and L1/L2/L3 subprocesses, contextual aliases, independent execution versions, replayable proposals, credential redaction, validated loading, corrupt-state recovery and interrupted replacement.

Latest dual-model configuration verification: **178 passed in 8.56s**. Git checks confirmed `.env` and `.env.local` are ignored while `.env.example` is trackable at workspace and project levels.

## Boundary assessment and trust

Boundary retains start_trigger, end_condition, confidence, status, evidence_ids and scope_ids. It adds explicit start_resolved/end_resolved flags, revision and change history. Empty endpoint text is permitted only while that endpoint is unresolved; confirmed boundaries must resolve both endpoints.

BoundaryAssessment records the exact boundary ID/revision/start/end, scope ID, scope justification, supporting As-Is evidence, reviewer/method, assessment status and proposal digest. Use policy.proposal_digest(operation_or_question) to bind a review to the exact proposal. Completed acceptance requires status=in_scope; pending, needs_review and out_of_scope are rejected for agent proposals. Explicit out_of_scope reviews are rejected even for trusted manual mutations. Supplied reviews must match their proposals; extra, duplicate or stale reviews are rejected.

Deterministic code does not establish semantic scope. A valid scope ID and an in_scope assertion are not proof that source text supports the activity. The trusted host supplies completed human reviews now, and may later integrate an implementation of BoundaryReviewer. The reviewer identity/status are supplied assertions, not cryptographic attestations. Do not let model-generated tool arguments masquerade as host-approved reviews.

Expose store.agent_tools() to a future agent. Its update_process_graph method fixes origin=agent and requires a completed assessment for every operation, including non-activity graph changes. It exposes graph reads, validation and assessed questions, but not initialization, confirmation recording, recovery or execution-state writes. Do not expose GraphStore's trusted origin selector as a model-controlled argument. Direct GraphStore.update_process_graph defaults to origin=manual to retain the existing local API; that path is for trusted callers only.

Agent boundary confirmation/revision must be a standalone transaction. This avoids mixing activities assessed against different boundary revisions in one batch. Changes invalidate old proposal reviews; the host must obtain fresh assessments. Existing activities are not automatically semantically re-reviewed when a boundary changes.

Question is a typed candidate with activity/start_boundary/end_boundary kind. propose_question(question, assessment, expected_version) checks the review and scope before queueing it in execution state. A start_boundary question requires start_resolved=False; an end_boundary question requires end_resolved=False. Ordinary questions require completed reviews as well. No question ranking or budget enforcement engine is implemented.

## Assumption provenance

Assumption preserves its original ID, rationale and evidence_ids. Its status and append-only Resolution history change only through resolve_assumption. Each resolution records an ID, rationale, supporting evidence and/or confirmation_id, target status and the actual previous status.

Supported transitions:

| Current | Allowed next states |
| --- | --- |
| unverified | confirmed, revised, rejected |
| revised | revised, confirmed, rejected |
| confirmed | revised, rejected |
| rejected | revised |

A reopening requires a new supported explicit resolution. Every resolution needs registered As-Is evidence or an existing UserConfirmation. Confirmation records include supplied statement, confirmed_by and a timezone-aware recorded_at; the code never invents a user answer. Only trusted manual callers can append confirmations. They cannot be updated or deleted.

Generic assumption update/delete is prohibited. New assumptions must start unverified without fabricated history. Generic node/flow/persona updates cannot remove or replace an original assumption reference, even after confirmation. History protects lineage across deletion/recreation and separate transactions. Confirmed inferred activities retain their assumption ID; consumers inspect the assumption's status rather than treating them as originally documented facts.

Rejected assumptions cannot support active graph elements. Reject and remove dependent elements in the same validated batch, or explicitly reopen the assumption. Original assumptions and their resolution history remain inspectable.

## Graph operations

Operation retains complete-record add/update/remove mutations. New explicit actions are:

| Action | Collection / target | Payload |
| --- | --- | --- |
| resolve_assumption | assumptions / existing ID | Resolution fields; previous_status is set by the store |
| confirm_boundary | boundary / contract ID | rationale, evidence_ids and/or confirmation_id |
| revise_boundary | boundary / contract ID | resolution support plus changed trigger/end, resolved flags, scope_ids or confidence |
| set_graph_state | graph / graph ID | complete, unresolved_issues and/or pool_ids |
| add | confirmations / new ID | UserConfirmation; trusted callers only |

Boundary changes increment its revision, preserve history and mark the graph incomplete. Every successful graph batch increments graph.version once, regardless of operation count. Final-state validation allows intermediate dangling references within a batch. Rejections preserve the graph and version history while recording the failed call.

Evidence is immutable and append-only. Exact quote offsets use Unicode string indices into source_text, not PDF byte positions. A source_id must identify one source_text within a graph. IDs and source authenticity remain caller responsibilities for the graph API; the independent Milestone 2 evidence registry has content-addressed source identities.

## Hierarchy, pools and personas

L1/L2/L3 are user-defined decomposition depths, not APQC taxonomy. A child references an activity at the preceding depth, in the same declared scope and BPMN pool. Children can use different lanes within that pool. Sequence-flow endpoints must share a pool and decomposition parent; cross-pool message flows are not implemented.

Complete-graph validation independently requires a root process in every declared pool, and one start plus at least one end and start-to-end reachability for each pool/parent decomposition. Drafts may remain disconnected with complete=False. Completion also requires a confirmed boundary, no unresolved issues and no unverified/revised assumptions; rejected records are retained but cannot remain attached to active elements.

Personas retain canonical IDs and organizational_context. Identical display labels are allowed in different contexts. Lane uniqueness remains one lane per persona per pool. Names/aliases are normalized and collisions within a context are rejected. resolve_persona_alias raises AmbiguousAlias for unknown or ambiguous matches; callers must provide context or a canonical ID. No roles are automatically merged.

## Separate execution state

ExecutionState contains run_id, its own version, graph_version, status, nonnegative remaining budgets, pending questions and Checkpoint records. It is persisted alongside, not inside, Graph.

- get_execution_state() reads the current state.
- save_execution_state(state, expected_state_version, expected_graph_version) is host-only and version checked.
- Initial state uses version=0 and expected_state_version=-1. Subsequent saves increment its version once.
- Checkpoints reference available graph versions. There is one run record per store.
- propose_question increments execution version without incrementing graph version.
- Execution state may remain tied to an older committed graph after a mutation. The host must explicitly refresh it and re-assess pending questions before using it against the new graph.

This is persistence scaffolding, not an orchestration engine. Budgets are not automatically decremented; scheduling, question rounds, ranking, user-answer handling and automatic resume remain later work.

## Trace and credential handling

All graph-agent calls record a tool_call_id, structured arguments, target IDs, outcomes, errors/findings, timestamp and graph version. Call IDs can be host-supplied or generated UUIDs. Duplicate and invalid IDs are rejected and recorded. Mutation outcomes distinguish committed, rolled_back, rejected and not_attempted operations.

Arguments retain payloads and assessments, allowing non-sensitive proposals to be reconstructed. The arguments.operations entries include payload_sha256 over canonical, redacted arguments; omit this metadata field when constructing Operation for replay. A replay still needs fresh expected versions and valid boundary reviews. Redacted proposals cannot be replayed exactly.

Sensitive credential fields, common key namespaces, recognizable sk-* keys, authorization strings and credential assignments are redacted from traces and rejected from graphs/execution state. Redaction is conservative and can reject credential-like legitimate text. It cannot detect every unlabeled secret; do not provide secrets in source documents, arbitrary labels or confirmations. API credentials and provider reasoning are excluded from persisted records.

When primary storage is unavailable or invalid, best-effort failures are written to a separate state.json.failures.jsonl sidecar. Storage exhaustion can prevent that audit write too. Diagnostic inspect_trace() does not itself append a graph-agent event.

## Persistence, compatibility and recovery

Format 2 stores graph, committed version snapshots, trace and execution_state with a checksum. Loading validates JSON uniqueness, envelope/schema types, graph invariants, ordered versions, immutable history, lineage, trace structure and execution references. The checksum detects accidental changes; it is not an authenticity signature or protection against a malicious editor who recomputes it.

Valid Milestone 1 format-1 files are decoded with defaults and upgraded on the next write. Legacy trace events receive stable legacy IDs; omitted historical payloads cannot be reconstructed. Files that violate hardened invariants require explicit review rather than silent repair. Invalid initialization can be retried on the same path without discarding its rejection trace; initialized graphs cannot be overwritten through initialize.

Before replacing the primary, the store atomically preserves the previous validated envelope in state.json.snapshot.json. Initial successful initialization preserves its valid initial state there. Both writes use flushed/fsynced temporary files and os.replace under a local exclusive lock. A snapshot failure aborts the primary commit; a primary failure leaves the prior primary and valid snapshot intact. Full-history snapshots favor inspectability over space efficiency.

Recovery procedure:

1. Stop all writers and agent runs. Record which primary and snapshot paths belong to this process.
2. Inspect state.json.lock if present. It records the PID and creation time. Verify the writer is no longer alive, accounting for PID reuse, before manually removing a stale lock. Recovery never steals a lock.
3. Call GraphStore(path).recover_from_snapshot(tool_call_id="unique-recovery-id"). It validates the separate snapshot and atomically replaces the primary. An invalid snapshot is rejected without overwriting the primary.
4. Inspect the recovery event and failures sidecar, re-read the graph/version and resume only after reconciling expected versions and external work. Restored execution state is marked paused and its version increments.
5. With no usable snapshot, preserve the available files for investigation and restore an independently verified external copy. There is no automatic guessing or partial JSON repair.

Recovery can roll back the latest primary commit and its trace entries to the preserved snapshot. It is an offline administrative operation, not safe concurrent rollback. Stale locks require manual verification. A crash can leave unused temporary files; they are not trusted recovery sources. Simultaneous loss of primary and snapshot, universal power-loss durability, malicious direct edits, distributed locking and automatic compaction are outside this prototype.

## LLM provider configuration

LLMConfig.from_env() reads OPENROUTER_API_KEY, OPENROUTER_FLASH_MODEL and OPENROUTER_PRO_MODEL from the process environment. All three are required; no actual model IDs are hardcoded. It does not use the legacy OPENROUTER_MODEL variable or direct OpenAI authentication. OPENROUTER_BASE_URL is optional and defaults to https://openrouter.ai/api/v1.

| Task | Default tier | Optional environment override |
| --- | --- | --- |
| extraction | Flash | OPENROUTER_EXTRACTION_MODEL |
| classification | Flash | OPENROUTER_CLASSIFICATION_MODEL |
| evidence_audit | Pro | OPENROUTER_EVIDENCE_AUDIT_MODEL |
| complex_reasoning | Pro | OPENROUTER_COMPLEX_REASONING_MODEL |

Call config.model_for(task, override=model_id) for an explicit per-call override. Precedence is per-call override, then task environment override, then tier default. Blank environment overrides use defaults; blank explicit overrides are rejected. Model IDs must come from deployment configuration or caller input. Flash and Pro are routing roles, not fixed model names; model quality and independence must be evaluated separately.

.env.example provides matching empty placeholders, including the optional task overrides. .env and .env.* are ignored by Git at both workspace and project level, with .env.example explicitly allowed. Copy the example to bpt-process-agent/.env for local credentials. The extraction CLI automatically loads that project-root file before reading configuration, regardless of the current working directory. Existing environment variables take precedence, including explicitly empty values. LLMConfig.from_env() itself remains an environment-only reader; --parse-only does not load credentials. Never print or serialize the config's API key; its dataclass representation omits the key.

Milestone 2 uses the OpenAI Python SDK through a thin provider/model client abstraction. Routine extraction/classification will use Flash; independent evidence audits and complex reasoning will use Pro. Verify structured-output support for the effective model selected for each task, including overrides and its provider route, before use. Capability checks run before extraction; no real provider requests are made by the automated test suite.

All automated SDK requests and capability checks must remain mocked; pytest must not make real API calls. API keys must never be hardcoded, printed, traced or committed. The independent ingestion package now implements extraction/auditing; it never exposes graph writes to an LLM.

## Milestone 2 — document ingestion and evidence extraction

Milestone 2 adds an evidence-only pipeline. It does not reconstruct graphs or implement
domain inference, clarification planning, BPMN export, a frontend, LangChain, LangGraph
or a database server. All existing graph/assumption/boundary/persona invariants remain
in the original modules; ingestion never opens a graph store.

### Files and architecture

- `ingestion/models.py`: source documents/blocks, exact citations, multi-tag facts,
  immutable fact revisions, extraction runs and audit findings.
- `ingestion/parsers.py`: PDF (pdfplumber), DOCX (python-docx) and PPTX (python-pptx),
  structured tables, headings, headers/footers and slide notes where available.
- `ingestion/validation.py`: strict response schemas, exact Unicode spans and validated loading.
- `ingestion/repository.py`: independent atomic/version-checked JSON evidence store,
  immutable content-addressed originals, append-only fact history and inspectable traces.
- `ingestion/llm.py`: replaceable StructuredClient protocol and thin OpenAI SDK adapter
  for OpenRouter. SDK results expose final structured answers/usage only, never reasoning.
- `ingestion/prompts.py` and `pipeline.py`: source-as-data policy, bounded batches,
  Flash extraction, fresh Pro audit, one controlled omission pass and verification audit.
- `ingestion/report.py`, `cli.py`, `smoke.py`: JSON/Markdown reports, CLI and opt-in live smoke.
- `tests/ingestion_fixtures.py`, `test_ingestion_*.py`, `conftest.py`: synthetic binary fixtures,
  mocked SDK/capability checks and an outbound-network guard.

Runtime dependencies are limited to three format parsers, the OpenAI SDK, jsonschema and python-dotenv for CLI configuration loading.
ReportLab is a test-only dependency to generate synthetic PDFs. No OCR or office renderer
is installed. The Python standard library handles identities, file storage, locks and reports.

The evidence store uses `registry.json` and a separate previous-valid
`registry.valid.json`. Originals live at `sources/<content-ID>.<format>`; parsed
blocks, model inputs/final responses, errors, findings and histories live in the registry.
IDs derive from source bytes/format and block contents/locators. Fact IDs derive from exact
source span anchors, so repeated runs on the same anchors do not create new facts. A
different assertion at an existing anchor creates an unresolved repeat-disagreement,
rather than replacing a previously accepted fact. Different documents remain separate,
even when their passages match: citations and conflicting policies are preserved.

Fact corrections append revisions. An unsupported fact is quarantined, never deleted;
classification/tag corrections require an explicit audit finding and source evidence.
Omissions are re-extracted only for the finding's cited source region. A second independent
audit must verify repairs before the finding can be resolved. Conflicts and ambiguity stay
open. Every model call is recorded as pending before execution and then completed/failed;
an interrupted call remains inspectable. No model receives a storage write tool.

### Install and test (PowerShell, Python 3.11+)

```powershell
Set-Location 'D:\9_Tiger Analytics\BPT\As-Is\bpt-process-agent'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

The existing workspace uses a local managed Python environment. If pip is absent there:

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e ".[test]"
```

All pytest tests mock model calls and metadata checks. The network guard also blocks
socket connections, including accidental requests. The live smoke command is outside
pytest and is never invoked automatically.

### Configure and extract real documents

Use the OpenRouter requirements below. Flash and Pro are configurable routing roles;
both model IDs must advertise strict structured outputs and be different for independent
audits. No model IDs are hardcoded. Per-task environment and per-run CLI overrides apply.
The extraction CLI automatically loads `bpt-process-agent/.env` using python-dotenv with
`override=False`; existing environment variables take precedence. Alternatively, export
values via your shell/launcher. The matching
`.env.example` is safe to copy locally; `.env` files are gitignored.

```powershell
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'OpenRouter API key' -AsSecureString)).Password
$env:OPENROUTER_FLASH_MODEL = Read-Host 'Flash model ID'
$env:OPENROUTER_PRO_MODEL = Read-Host 'Pro model ID'
$env:OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'

# One or several real documents; replace these paths with your files.
.\.venv\Scripts\python.exe -m bpt_process_agent.ingestion.cli 'C:\Documents\process.pdf' 'C:\Documents\sop.docx' 'C:\Documents\quotation.pptx' --store .\evidence-store --output .\evidence-output

# Offline parsing/search preparation, without an API key.
.\.venv\Scripts\python.exe -m bpt_process_agent.ingestion.cli 'C:\Documents\process.pdf' --parse-only --store .\evidence-store --output .\evidence-output

# Optional live smoke: explicitly opted in, maximum eight actions including metadata checks.
.\.venv\Scripts\python.exe -m bpt_process_agent.ingestion.smoke --allow-paid-api 'C:\Documents\process.pdf'
```

The installed `bpt-extract` entry point accepts the same extraction options.
Use `--extraction-model <configured-ID>` / `--audit-model <configured-ID>` to override
models for a run, `--batch-chars 16000` for source batch size and `--max-calls 100`
for the total action budget (including two capability checks). These are bounded
pipeline settings, not an orchestration engine. Calls may incur provider charges.

Outputs are `evidence-output/evidence.json` (run, source blocks, findings, complete
fact revision histories) and `evidence-output/evidence-report.md` (readable current
facts, exact source passages, parsing limitations, conflicts and errors). Inspect
`evidence-store/registry.json` for the full trace. Exit codes: 0 for audited parsed
content or usable offline parsing; 2 for partial/failed coverage; 1 for setup/storage
failure. Partial reports are still generated. Originals and parsed source blocks remain
available through `EvidenceStore.search_sources(query)` for later investigation.

Structured-output verification uses OpenRouter's
[model endpoint metadata](https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model).
The adapter permits only routes advertising both `structured_outputs` and
`response_format`, pins qualifying providers and sets `require_parameters=true`.
It requests `json_schema` with `strict=true`, following
[OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
and validates schema/citations locally. Unsupported or unverified routes fail explicitly;
there is no silent JSON-mode fallback. Metadata is an advertised capability, not proof
that every schema keyword is supported; provider rejection is recorded as a failed call.

### Coverage and parser limitations

- PDF text/tables use physical page locators; PPTX text/tables/notes use slide locators;
  DOCX uses ordered blocks/headings because physical pagination requires rendering.
- Scans, images, chart/diagram meaning, connectors, annotations, tracked changes,
  embedded objects, some content controls and complex table layout are not fully
  interpreted. Explicit issues mark coverage incomplete. No OCR/vision claims.
- Files are limited to 25 MiB, expanded DOCX/PPTX archives to 100 MiB. Read/parse
  failures remain recorded; later documents/pages/blocks are attempted where possible.
- Every supplied source character is batched, without silent truncation. Large blocks
  can split an assertion/table; cross-batch conflicts/relations are not globally audited
  and produce an explicit unresolved gap. Increase batch size only within model limits.
- Audit quality depends on the configured models. Exact quotation validation proves
  that a passage exists; it does not prove the statement's semantic fidelity or atomicity.
  The label `audited_parsed_content` is an LLM audit outcome, never guaranteed coverage.
- Anchor-based dedup is deterministic, not semantic merging. Alternate/overlapping
  quotation spans may yield duplicates. Conflicting sources and ambiguous statements
  require later human review; no source-priority rule is inferred.
- This stage registers source evidence broadly. Boundary gates remain required before
  later graph changes/questions; the extractor does not semantically set a boundary.
- Model calls may fail for context/output limits, provider availability or unsupported
  schema keywords. No retry/fallback loops; errors preserve incomplete status.
- Credential recognition is conservative, not universal. Recognizable credentials
  in parsed text are rejected and that document is not archived. Do not upload secrets;
  originals can contain opaque content the parser cannot inspect. Traces deliberately
  retain non-secret source text for replay/investigation, so protect local artifacts.
- One machine, local fail-fast locking, full snapshot rewrites and no automatic resume/
  compaction. Checksums detect accidental changes, not malicious recomputation.
  A separately preserved snapshot and matching originals are needed for recovery.

### Evidence recovery

1. Stop writers. Preserve the corrupt/interrupted primary and `sources/` for investigation.
2. Inspect `.evidence.lock`; verify no live writer before manually removing a stale lock.
   Temporary `.commit-*` files and orphan source blobs are not trusted snapshots.
3. Select a separately preserved valid registry snapshot with its matching source files.
   The automatic `registry.valid.json` is the previous valid state, so recovery can lose
   the latest run/trace entries. Regularly copy registry plus originals to a separate backup.
4. Run explicitly after preserving the corrupt primary:

```powershell
.\.venv\Scripts\python.exe -c "from bpt_process_agent.ingestion.repository import EvidenceStore; EvidenceStore('evidence-store').recover('evidence-store/registry.valid.json')"
```

5. Loading/recovery verifies schema, content checksums, exact citations and source archive
   hashes. Invalid snapshots are rejected. Review pending calls, histories and versions
   before starting a new extraction run. No automatic JSON repair or replay of paid calls.


## Milestone 2 verification

Complete suite: **251 passed in 21.57s** on Python 3.11.15: 178 existing graph/configuration tests and 73 new ingestion tests. The 73 new tests also passed independently in 14.57s. Editable installation and the `bpt-extract --help` entry point passed. Automated SDK/capability requests were mocked and socket connections blocked. No live LLM completion or paid smoke test was run; actual deployment model compatibility is checked when the CLI runs.
