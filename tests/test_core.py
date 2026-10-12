from copy import deepcopy
import pytest
from bpt_process_agent.schemas import Boundary, Evidence, Flow, Graph, Lane, Node, Persona, Assumption
from bpt_process_agent.validation import validate

@pytest.fixture
def graph():
    text = "Sales receives a quotation request, collects vendor rates, compiles a quote and sends it to the customer."
    g = Graph("quotation", Boundary("boundary", "Request received", "Quote sent", "provisional", .8, ["e1"], ["quotation"]), pool_ids=["business"])
    g.evidence["e1"] = Evidence("e1", "fixture", text, 0, len(text), text)
    g.personas["sales"] = Persona("sales", "Sales", ["seller"], ["e1"])
    g.lanes["sales-lane"] = Lane("sales-lane", "business", "sales")
    for id, kind in [("start", "start"), ("collect", "activity"), ("compile", "activity"), ("send", "activity"), ("end", "end")]:
        g.nodes[id] = Node(id, id, kind, "quotation", "sales-lane", ["e1"])
    for index, (source, target) in enumerate(zip(list(g.nodes), list(g.nodes)[1:])):
        id = f"f{index}"
        g.flows[id] = Flow(id, source, target, "quotation", ["e1"])
    return g


def codes(graph):
    return {f.code for f in validate(graph)}


def test_round_trip(graph):
    assert Graph.from_dict(graph.to_dict()) == graph
    assert validate(graph) == []

@pytest.mark.parametrize("field,value", [("kind", "script"), ("level", "APQC"), ("evidence_ids", "e1")])
def test_bad_schema(graph, field, value):
    setattr(graph.nodes["collect"], field, value)
    assert "schema" in codes(graph)

@pytest.mark.parametrize("classification", ["to_be", "problem", "context"])
def test_not_as_is(graph, classification):
    graph.evidence["e1"].classification = classification
    assert "invalid_evidence" in codes(graph)


def test_exact_provenance(graph):
    graph.evidence["e1"].quote = "fabricated quote"
    assert "source_span" in codes(graph)

@pytest.mark.parametrize("mutation,expected", [
    (lambda g: setattr(g.flows["f0"], "target_id", "missing"), "flow_reference"),
    (lambda g: setattr(g.nodes["collect"], "scope_id", "acquisition"), "out_of_boundary"),
    (lambda g: setattr(g.nodes["collect"], "lane_id", "missing"), "node_lane"),
    (lambda g: setattr(g.nodes["collect"], "evidence_ids", []), "missing_evidence"),
    (lambda g: setattr(g.nodes["collect"], "assumption_id", "missing"), "assumption_reference"),
    (lambda g: setattr(g.nodes["collect"], "parent_id", "collect"), "hierarchy"),
    (lambda g: setattr(g.boundary, "confidence", 2), "boundary"),
    (lambda g: setattr(g.boundary, "start_trigger", ""), "boundary"),
    (lambda g: setattr(g, "version", -1), "graph_identity"),
    (lambda g: setattr(g, "version", True), "schema"),
    (lambda g: setattr(g.personas["sales"], "id", "other"), "identity"),
])
def test_invalid_graph(graph, mutation, expected):
    mutation(graph)
    assert expected in codes(graph)


def test_alias_and_lane_uniqueness(graph):
    graph.personas["other"] = Persona("other", "Other", [" SELLER "])
    graph.lanes["duplicate"] = Lane("duplicate", "business", "sales")
    assert {"persona_alias", "duplicate_lane"} <= codes(graph)


def test_inference_requires_rationale_and_support(graph):
    graph.nodes["collect"].evidence_ids = []
    graph.nodes["collect"].assumption_id = "a1"
    graph.assumptions["a1"] = Assumption("a1", "", [])
    assert {"assumption_rationale", "missing_evidence"} <= codes(graph)
    graph.assumptions["a1"] = Assumption("a1", "Low-risk intermediate step", ["e1"])
    assert validate(graph) == []


def test_complete_and_unreachable(graph):
    graph.boundary.status = "confirmed"
    graph.complete = True
    assert validate(graph) == []
    graph.flows.pop("f1")
    assert "unreachable" in codes(graph)


def test_incomplete_is_explicit(graph):
    graph.complete = True
    graph.unresolved_issues = ["Boundary requires confirmation"]
    assert "incomplete" in codes(graph)


def test_event_direction(graph):
    graph.flows["bad"] = Flow("bad", "end", "start", "quotation", ["e1"])
    assert "event_direction" in codes(graph)


def test_cross_pool(graph):
    graph.pool_ids.append("customer")
    graph.lanes["customer"] = Lane("customer", "customer", "sales")
    graph.nodes["end"].lane_id = "customer"
    assert "cross_pool_flow" in codes(graph)


def test_persona_requires_provenance(graph):
    graph.personas["sales"].evidence_ids = []
    assert "missing_evidence" in codes(graph)


def test_valid_hierarchy(graph):
    graph.nodes["child"] = Node("child", "Child", "activity", "quotation", "sales-lane", ["e1"], parent_id="collect", level="L2")
    assert validate(graph) == []
    graph.flows["cross"] = Flow("cross", "collect", "child", "quotation", ["e1"])
    assert "flow_hierarchy" in codes(graph)


def test_complete_gateway_and_events(graph):
    graph.complete = True
    graph.boundary.status = "confirmed"
    graph.nodes["compile"].kind = "gateway"
    assert "gateway_degree" in codes(graph)
    graph.nodes["start"].kind = "activity"
    assert "events" in codes(graph)


def test_complete_empty_graph(graph):
    graph.complete = True
    graph.boundary.status = "confirmed"
    graph.nodes.clear()
    graph.flows.clear()
    assert "empty_graph" in codes(graph)


def test_scope_and_pool_ids(graph):
    graph.boundary.scope_ids.append("quotation")
    graph.pool_ids.append("business")
    graph.lanes["sales-lane"].persona_id = "unknown"
    assert {"boundary_scope", "pools", "lane_reference"} <= codes(graph)


def test_scope_mismatch(graph):
    graph.boundary.scope_ids.append("other")
    graph.flows["f0"].scope_id = "other"
    assert "flow_scope" in codes(graph)
