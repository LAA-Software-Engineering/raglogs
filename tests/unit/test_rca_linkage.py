"""Unit tests for trace-derived service linkage (#82 T1). No DB."""
from src.core.rca.linkage import ServiceGraph, service_graph_from_spans


def _span(span_id, parent, service):
    return (span_id, parent, service)


class TestServiceGraphFromSpans:
    def test_builds_caller_callee_edges(self):
        # frontend -> cart -> redis ; frontend -> payment
        spans = [
            _span("f", None, "frontend"),
            _span("c", "f", "cart"),
            _span("r", "c", "redis"),
            _span("p", "f", "payment"),
        ]
        g = service_graph_from_spans(spans)
        assert g.services == {"frontend", "cart", "redis", "payment"}
        assert ("frontend", "cart") in g.edges
        assert ("cart", "redis") in g.edges
        assert ("frontend", "payment") in g.edges

    def test_same_service_parent_child_makes_no_self_edge(self):
        g = service_graph_from_spans([_span("a", None, "svc"), _span("b", "a", "svc")])
        assert g.edges == set()
        assert g.services == {"svc"}

    def test_empty_when_no_spans(self):
        assert service_graph_from_spans([]).empty


class TestReachableAndLinked:
    def _g(self):
        return service_graph_from_spans([
            _span("f", None, "frontend"),
            _span("c", "f", "cart"),
            _span("r", "c", "redis"),
            _span("p", "f", "payment"),
        ])

    def test_reachable_follows_dependency_direction(self):
        g = self._g()
        assert g.reachable("frontend", "redis")  # transitive caller->callee
        assert not g.reachable("redis", "frontend")
        assert g.reachable("cart", "cart")  # reflexive

    def test_linked_is_either_direction_or_same(self):
        g = self._g()
        assert g.linked("frontend", "redis")  # upstream
        assert g.linked("redis", "frontend")  # downstream (either direction)
        assert g.linked("cart", "cart")

    def test_unrelated_services_not_linked(self):
        # cart and payment are siblings (no path between them)
        g = self._g()
        assert not g.linked("cart", "payment")

    def test_unknown_service_not_linked(self):
        g = self._g()
        assert not g.linked("frontend", "not-in-any-trace")

    def test_empty_graph_only_links_same_service(self):
        g = ServiceGraph()
        assert g.linked("a", "a")
        assert not g.linked("a", "b")
