# BPT Process Reconstruction Agent — Prototype Specification

## Objective
Build a standalone, inspectable Python prototype that reconstructs an As-Is business process from sparse or detailed source documents into a canonical, evidence-linked graph, with BPMN 2.0 export later. Prioritize correctness, traceability and explicit uncertainty over polished UI.

## Scope
- Milestone 2 input: one or several PDF/PPTX/DOCX documents; archive originals and immutable parsed source blocks, including explicit parsing limitations.
- Output: canonical JSON process graph, evidence registry, assumption registry, clarification questions, validation report, execution trace. BPMN XML and visualization are later milestones.
- No frontend, hosted database, LangChain or LangGraph in MVP. Use Python, OpenRouter via the OpenAI Python SDK, local JSON files, pytest.

## LLM provider configuration (Milestone 2 onward)

- Use OpenRouter as the LLM API provider, not direct OpenAI API authentication.
- Default the configurable base_url to `https://openrouter.ai/api/v1`.
- Read credentials exclusively from `OPENROUTER_API_KEY` and both model IDs from `OPENROUTER_FLASH_MODEL` and `OPENROUTER_PRO_MODEL`. Do not fall back to `OPENROUTER_MODEL` or `OPENAI_API_KEY`, or hardcode model IDs.
- Route routine extraction and classification to Flash; route independent evidence audits and complex reasoning to Pro. Each task may override its default through task-specific configuration or an explicit per-call model ID. Per-call overrides take precedence over task environment overrides, which take precedence over tier defaults.
- Optional environment overrides are `OPENROUTER_EXTRACTION_MODEL`, `OPENROUTER_CLASSIFICATION_MODEL`, `OPENROUTER_EVIDENCE_AUDIT_MODEL` and `OPENROUTER_COMPLEX_REASONING_MODEL`. Read the configurable endpoint from `OPENROUTER_BASE_URL`.
- Keep `.env` and `.env.*` gitignored, allow `.env.example`, and maintain matching empty credential/model placeholders in the example.
- Use the OpenAI Python SDK with an explicitly configured base_url and OpenRouter key. The SDK is the transport client, not the provider.
- Keep a thin, replaceable LLM client abstraction separate from agent policy, deterministic validation and graph persistence so providers and models can change later.
- Before using structured output, verify support for the effective task model (including any override) and provider route against current OpenRouter documentation or capability metadata. Do not assume uniform support across models. Report unsupported or unverified capabilities explicitly rather than silently changing output guarantees.
- Keep all automated tests mocked, including SDK requests and capability checks. Pytest must never make real API calls.
- Never hardcode, print, trace or commit API keys. Keep credentials out of graph state, execution state, exceptions and artifacts.
- Milestone 2 is authorized for document ingestion, evidence extraction and independent evidence audit only. Graph reconstruction, domain inference and clarification planning remain later work.

## Invariants
1. Establish a process boundary contract (start trigger, end condition, confidence/status, supporting evidence) before graph reconstruction. Clarifications and inferred activities must remain strictly within this boundary, except questions needed to define an ambiguous boundary itself.
2. Distinguish As-Is facts from To-Be recommendations, problems and contextual descriptions. Never present future-state ideas as existing steps.
3. All sourced process facts have stable IDs and exact source provenance; do not silently discard process-relevant statements.
4. All inferred graph elements explicitly reference an assumption record and supporting rationale. Never silently convert assumptions into facts.
5. One canonical persona ID maps to at most one lane per pool; alias resolution is controlled. Different roles are not merged merely due to similar labels.
6. A graph is versioned. Mutations are atomic, validated and logged. Failed batches leave the previous graph unchanged.
7. Validate structural correctness separately from semantic/evidence fidelity.
8. Clarification questions concern only in-boundary process activities, decisions, roles, exceptions or the start/end boundary itself. Rank by process impact and information gain; default 3 questions per round and 2 rounds total.
9. Inference policy = Balanced: infer strongly implied or low-risk domain-standard intermediate steps, label as unverified; ask about material organization-specific rules; defer unsupported details. Search evidence before inference.
10. Preserve unresolved issues; budget exhaustion results in an explicitly incomplete draft, not false success.
11. L1/L2/L3 mean user-defined levels of process decomposition/detail, NOT APQC levels. Generate from one canonical hierarchical model rather than independent disconnected maps.

## Initial tool contracts
- `get_evidence(filters)` — retrieve fact records.
- `search_sources(query)` — find supporting source spans.
- `get_process_graph()` — read current version.
- `update_process_graph(operations, expected_version)` — atomic add/update/remove persona, activity, gateway, flow etc.; enforce references and invariants.
- `validate_process_graph()` — structural issues, plus separate semantic review findings.
- `record_assumption(candidate, basis, fact_ids)` — persist provenance.
- `propose_question(question, uncertainty_ids)` — candidate clarification, subject to scope and budget.
- `get_domain_knowledge(query)` — optional retrieval, initially absent or lightweight.

## Agent execution
1. Extract evidence, classify source statements and assign stable IDs.
2. Identify boundary contract and record uncertainty.
3. Build initial graph using tools.
4. Validate; investigate source gaps first; apply balanced inference or propose clarifications.
5. Iterate within configured tool/LLM call limits. Persist state and traces for pause/resume.
6. Export JSON and validation/uncertainty report; BPMN compilation in a later milestone.

## Acceptance tests
- Sparse quotation slide: collect vendor rates, compile branded quote, send to customer; future-state automation ideas excluded from As-Is.
- Start/end constrained: no customer acquisition or post-quotation fulfillment questions.
- Duplicate persona aliases do not create duplicate lanes.
- Unsupported approval policy or fixed markup never silently inserted as fact.
- Broken node references and malformed graph updates rejected atomically.
- Agent can stop at question budget and produce an incomplete, transparent draft.

## Implementation milestones
1. Schemas, persistence, boundary contract and deterministic graph validator.
2. PDF/PPTX/DOCX ingestion and atomic evidence extraction with multi-tags, As-Is/To-Be/ambiguous classification, exact citations, versioned evidence, independent Pro coverage audit, controlled omission recovery, reports and CLI. No graph reconstruction.
3. Graph mutation tools and execution trace.
4. LLM reconstruction loop and inference policy.
5. Clarification planner, pause/resume and evaluation fixtures.
6. BPMN 2.0 export and basic rendering.
