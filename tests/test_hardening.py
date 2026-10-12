from copy import deepcopy
from dataclasses import asdict
import pytest
from test_core import graph
from bpt_process_agent.schemas import (Assumption, BoundaryAssessment, Flow, Lane,
                                      Node, Operation, Persona, Question)
from bpt_process_agent.policy import (AmbiguousAlias, PolicyViolation, check_assessment,
                                     check_question, proposal_digest, resolve_persona_alias)
from bpt_process_agent.validation import validate

def assessment(graph, proposal, **changes):
    value = BoundaryAssessment("review", graph.boundary.id, graph.boundary.revision,
                               graph.boundary.start_trigger, graph.boundary.end_condition,
                               "quotation", "Proposal concerns preparation and delivery of quotation",
                               "in_scope", ["e1"], proposal_digest(proposal), "test reviewer")
    for key, item in changes.items():
        setattr(value, key, item)
    return value

def node_edit(graph, **changes):
    node = asdict(graph.nodes["collect"])
    node.update(changes)
    return Operation("update", "nodes", "collect", node)

def test_assessment_contract(graph):
    op = node_edit(graph, label="Collect vendor rates")
    check_assessment(graph, op, assessment(graph, op))

@pytest.mark.parametrize("changes", [
    {"status": "pending"}, {"status": "needs_review"}, {"status": "out_of_scope"},
    {"boundary_revision": 8}, {"start_trigger": "Different"}, {"end_condition": "Different"},
    {"proposal_digest": "different"}, {"evidence_ids": []}, {"evidence_ids": ["missing"]},
    {"scope_id": "acquisition"}, {"reviewer": ""}, {"scope_justification": ""},
])
def test_assessment_rejections(graph, changes):
    op = node_edit(graph)
    with pytest.raises(PolicyViolation):
        check_assessment(graph, op, assessment(graph, op, **changes))

def test_assessment_does_not_claim_semantic_truth(graph):
    # Deterministic code validates the supplied review, not activity meaning.
    op = node_edit(graph, label="Acquire customers")
    check_assessment(graph, op, assessment(graph, op))
    with pytest.raises(PolicyViolation):
        check_assessment(graph, op, assessment(graph, op, status="out_of_scope"))

@pytest.mark.parametrize("kind,flag", [("start_boundary", "start_resolved"), ("end_boundary", "end_resolved")])
def test_boundary_definition_questions(graph, kind, flag):
    q = Question("q", "What defines this endpoint?", "quotation", kind)
    with pytest.raises(PolicyViolation):
        check_question(graph, q, assessment(graph, q))
    setattr(graph.boundary, flag, False)
    check_question(graph, q, assessment(graph, q))

def add_process(graph, prefix, pool, parent=None, level="L1"):
    for suffix, kind in [("s", "start"), ("a", "activity"), ("e", "end")]:
        id = prefix + suffix
        graph.nodes[id] = Node(id, id, kind, "quotation", pool, ["e1"], parent_id=parent, level=level)
    for suffix, source, target in [("1", prefix+"s", prefix+"a"), ("2", prefix+"a", prefix+"e")]:
        id = prefix + suffix
        graph.flows[id] = Flow(id, source, target, "quotation", ["e1"])

def test_multi_pool_complete_hierarchy(graph):
    graph.pool_ids.append("other")
    graph.lanes["other"] = Lane("other", "other", "sales")
    add_process(graph, "other", "other")
    add_process(graph, "child", "sales-lane", "collect", "L2")
    add_process(graph, "grand", "sales-lane", "childa", "L3")
    graph.complete = True
    graph.boundary.status = "confirmed"
    assert validate(graph) == []
    assert type(graph).from_dict(graph.to_dict()) == graph

def test_cross_pool_subprocess_rejected(graph):
    graph.pool_ids.append("other")
    graph.lanes["other"] = Lane("other", "other", "sales")
    add_process(graph, "child", "other", "collect", "L2")
    assert "hierarchy_pool" in {f.code for f in validate(graph)}

def test_contextual_personas_and_alias_ambiguity(graph):
    graph.personas["sales"].label = "Manager"
    graph.personas["sales"].aliases = ["lead"]
    graph.personas["sales"].organizational_context = "Sales"
    graph.personas["finance"] = Persona("finance", "Manager", ["lead"], ["e1"], organizational_context="Finance")
    assert validate(graph) == []
    with pytest.raises(AmbiguousAlias):
        resolve_persona_alias(graph, "lead")
    assert resolve_persona_alias(graph, "lead", "Finance") == "finance"

def test_assumption_history_cannot_be_fabricated(graph):
    graph.assumptions["a"] = Assumption("a", "Original inference", ["e1"], status="confirmed")
    assert "assumption_history" in {f.code for f in validate(graph)}
