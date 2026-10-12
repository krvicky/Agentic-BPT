"""Deterministic structure and provenance checks; no semantic truth claims."""
from datetime import datetime
from .schemas import ASSUMPTION_TRANSITIONS, Finding, Graph, matches_schema


def validate(graph: Graph) -> list[Finding]:
    issues: list[Finding] = []
    def fail(code: str, id: str, message: str, category: str = "structural") -> None:
        issues.append(Finding(code, id, message, category))
    if not matches_schema(graph, Graph):
        return [Finding("schema", "graph", "Record types or enum values are invalid")]
    b = graph.boundary
    if not graph.id.strip() or graph.version < 0:
        fail("graph_identity", graph.id, "Graph needs an ID and nonnegative integer version")
    if not b.id.strip() or (b.start_resolved and not b.start_trigger.strip()) or (b.end_resolved and not b.end_condition.strip()) or not b.scope_ids or not 0 <= b.confidence <= 1:
        fail("boundary", b.id, "Boundary needs trigger, end, scope and confidence in [0, 1]")
    if len(b.scope_ids) != len(set(b.scope_ids)) or any(not s.strip() for s in b.scope_ids):
        fail("boundary_scope", b.id, "Scope IDs must be unique and nonempty")
    if len(graph.pool_ids) != len(set(graph.pool_ids)) or any(not p.strip() for p in graph.pool_ids):
        fail("pools", graph.id, "Pool IDs must be unique and nonempty")
    for collection in (graph.evidence, graph.assumptions, graph.personas, graph.lanes, graph.nodes, graph.flows, graph.confirmations):
        for key, record in collection.items():
            if not key.strip() or key != record.id:
                fail("identity", key, "Registry key must equal a nonempty record ID")
    sources: dict[str, str] = {}
    for e in graph.evidence.values():
        if e.source_id in sources and sources[e.source_id] != e.source_text:
            fail("source_identity", e.id, "A source ID must identify one immutable source text", "provenance")
        sources[e.source_id] = e.source_text
        if not e.source_id.strip() or not 0 <= e.start < e.end <= len(e.source_text) or e.source_text[e.start:e.end] != e.quote:
            fail("source_span", e.id, "Quote must exactly match a nonempty source span", "provenance")
    def evidence(ids: list[str], id: str, required: bool = True) -> None:
        if required and not ids:
            fail("missing_evidence", id, "Supporting As-Is evidence is required", "provenance")
        for eid in ids:
            e = graph.evidence.get(eid)
            if e is None or e.classification != "as_is":
                fail("invalid_evidence", id, "Evidence must reference a registered As-Is statement", "provenance")
    evidence(b.evidence_ids, b.id)
    if b.revision != len(b.history) or any(h.revision != i + 1 for i, h in enumerate(b.history)):
        fail("boundary_history", b.id, "Boundary revision must match its ordered history")
    if b.status == "confirmed" and not (b.start_resolved and b.end_resolved):
        fail("boundary_unresolved", b.id, "Confirmed boundary must resolve both endpoints")
    def support(ids: list[str], confirmation_id: str | None, id: str) -> None:
        evidence(ids, id, confirmation_id is None)
        if confirmation_id is not None and confirmation_id not in graph.confirmations:
            fail("confirmation_reference", id, "Resolution needs a recorded confirmation", "provenance")
    for h in b.history:
        support(h.evidence_ids, h.confirmation_id, b.id)
        if not h.rationale.strip():
            fail("boundary_history", b.id, "Boundary changes require rationale")
    for c in graph.confirmations.values():
        try:
            timestamp = datetime.fromisoformat(c.recorded_at)
            valid_time = timestamp.tzinfo is not None
        except ValueError:
            valid_time = False
        if not c.statement.strip() or not c.confirmed_by.strip() or not valid_time:
            fail("confirmation_record", c.id, "Confirmation needs statement, recorder and timezone-aware timestamp", "provenance")
    for a in graph.assumptions.values():
        if not a.rationale.strip():
            fail("assumption_rationale", a.id, "Inference requires a rationale", "provenance")
        evidence(a.evidence_ids, a.id)
        previous = "unverified"
        seen_resolutions: set[str] = set()
        for resolution in a.history:
            support(resolution.evidence_ids, resolution.confirmation_id, a.id)
            if not resolution.id.strip() or resolution.id in seen_resolutions or not resolution.rationale.strip() or resolution.previous_status != previous or resolution.status not in ASSUMPTION_TRANSITIONS[previous]:
                fail("assumption_history", a.id, "Invalid assumption transition history", "provenance")
            seen_resolutions.add(resolution.id)
            previous = resolution.status
        if a.status != previous:
            fail("assumption_history", a.id, "Status must match preserved resolution history", "provenance")
    aliases: dict[tuple[str, str], str] = {}
    for p in graph.personas.values():
        evidence(p.evidence_ids, p.id, p.assumption_id is None)
        if p.assumption_id is not None and p.assumption_id not in graph.assumptions:
            fail("assumption_reference", p.id, "Unknown assumption", "provenance")
        elif p.assumption_id is not None and graph.assumptions[p.assumption_id].status == "rejected":
            fail("rejected_assumption", p.id, "Active persona cannot rely on a rejected assumption", "provenance")
        if not p.label.strip():
            fail("persona_label", p.id, "Persona label is required")
        for alias in [p.label, *p.aliases]:
            normalized = alias.strip().casefold()
            key = (p.organizational_context.strip().casefold(), normalized)
            if not normalized or key in aliases and aliases[key] != p.id:
                fail("persona_alias", p.id, "Alias collision requires explicit resolution")
            aliases[key] = p.id
    pairs: set[tuple[str, str]] = set()
    for lane in graph.lanes.values():
        if lane.pool_id not in graph.pool_ids or lane.persona_id not in graph.personas:
            fail("lane_reference", lane.id, "Lane references an unknown pool or persona")
        pair = (lane.pool_id, lane.persona_id)
        if pair in pairs:
            fail("duplicate_lane", lane.id, "Only one lane per persona per pool")
        pairs.add(pair)
    for element in [*graph.nodes.values(), *graph.flows.values()]:
        if element.scope_id not in b.scope_ids:
            fail("out_of_boundary", element.id, "Element scope is outside the boundary")
        evidence(element.evidence_ids, element.id, element.assumption_id is None)
        if element.assumption_id is not None:
            assumption = graph.assumptions.get(element.assumption_id)
            if assumption is None:
                fail("assumption_reference", element.id, "Unknown assumption", "provenance")
            elif assumption.status == "rejected":
                fail("rejected_assumption", element.id, "Active elements cannot rely on a rejected assumption", "provenance")
    incoming = {id: set() for id in graph.nodes}
    outgoing = {id: set() for id in graph.nodes}
    for flow in graph.flows.values():
        if flow.source_id not in graph.nodes or flow.target_id not in graph.nodes:
            fail("flow_reference", flow.id, "Flow endpoint does not exist")
            continue
        source, target = graph.nodes[flow.source_id], graph.nodes[flow.target_id]
        if source.scope_id != flow.scope_id or target.scope_id != flow.scope_id:
            fail("flow_scope", flow.id, "Flow and endpoints must share a scope")
        if source.parent_id != target.parent_id:
            fail("flow_hierarchy", flow.id, "Flow endpoints must share a decomposition parent")
        sl, tl = graph.lanes.get(source.lane_id), graph.lanes.get(target.lane_id)
        if sl and tl and sl.pool_id != tl.pool_id:
            fail("cross_pool_flow", flow.id, "Sequence flows cannot cross pools")
        outgoing[source.id].add(target.id)
        incoming[target.id].add(source.id)
    for n in graph.nodes.values():
        if not n.label.strip() or n.lane_id not in graph.lanes:
            fail("node_lane", n.id, "Node needs a label and registered lane")
        parent = graph.nodes.get(n.parent_id) if n.parent_id else None
        if n.parent_id and (parent is None or parent.kind != "activity" or parent.scope_id != n.scope_id or {"L1": "L2", "L2": "L3"}.get(parent.level) != n.level):
            fail("hierarchy", n.id, "Parent must be an activity at the preceding level in the same scope")
        if parent and n.lane_id in graph.lanes and parent.lane_id in graph.lanes and graph.lanes[n.lane_id].pool_id != graph.lanes[parent.lane_id].pool_id:
            fail("hierarchy_pool", n.id, "Subprocess children must belong to their parent pool")
        if not n.parent_id and n.level != "L1":
            fail("hierarchy", n.id, "Non-root levels require a parent")
        if n.kind == "start" and incoming[n.id] or n.kind == "end" and outgoing[n.id]:
            fail("event_direction", n.id, "Start cannot receive and end cannot send sequence flow")
    if graph.complete:
        if graph.unresolved_issues or b.status != "confirmed" or any(a.status in ("unverified", "revised") for a in graph.assumptions.values()):
            fail("incomplete", graph.id, "Completion requires confirmed boundary and resolved uncertainty")
        groups = {(graph.lanes[n.lane_id].pool_id, n.parent_id) for n in graph.nodes.values() if n.lane_id in graph.lanes}
        if set(graph.pool_ids) - {pool for pool, parent in groups if parent is None}:
            fail("empty_pool", graph.id, "Every complete pool needs a root process")
        if not groups:
            fail("empty_graph", graph.id, "Complete graph cannot be empty")
        for pool_id, parent_id in sorted(groups, key=lambda p: (p[0], p[1] or "")):
            nodes = {n.id: n for n in graph.nodes.values() if n.parent_id == parent_id and n.lane_id in graph.lanes and graph.lanes[n.lane_id].pool_id == pool_id}
            starts = {n.id for n in nodes.values() if n.kind == "start"}
            ends = {n.id for n in nodes.values() if n.kind == "end"}
            if len(starts) != 1 or not ends:
                fail("events", parent_id or pool_id, "Each pool/decomposition requires one start and at least one end")
            def reachable(seeds: set[str], links: dict[str, set[str]]) -> set[str]:
                seen, pending = set(seeds), list(seeds)
                while pending:
                    for target in sorted(links[pending.pop()]):
                        if target not in seen:
                            seen.add(target)
                            pending.append(target)
                return seen
            if set(nodes) - reachable(starts, outgoing) or set(nodes) - reachable(ends, incoming):
                fail("unreachable", parent_id or pool_id, "Every node must be on a start-to-end path")
            for n in nodes.values():
                if n.kind == "gateway" and max(len(incoming[n.id]), len(outgoing[n.id])) < 2:
                    fail("gateway_degree", n.id, "Gateway must split or merge")
    return issues
