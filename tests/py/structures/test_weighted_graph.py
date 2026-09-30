import pytest

from cantrack_api.structures import ShortestPath, WeightedGraph


def test_single_edge():
    g = WeightedGraph()
    g.add_edge("home", "dog-a", 5)
    result = g.shortest_path("home", "dog-a")
    assert isinstance(result, ShortestPath)
    assert result.distance == 5
    assert result.path == ["home", "dog-a"]


def test_picks_shorter_path_not_fewer_hops():
    g = WeightedGraph()
    g.add_edge("home", "b", 10)
    g.add_edge("home", "c", 1)
    g.add_edge("c", "b", 1)
    result = g.shortest_path("home", "b")
    assert result.distance == 2
    assert result.path == ["home", "c", "b"]


def test_no_path_raises_value_error():
    g = WeightedGraph()
    g.add_edge("home", "dog-a", 5)
    g.add_node("island")
    with pytest.raises(ValueError):
        g.shortest_path("home", "island")


def test_edges_are_directed():
    g = WeightedGraph()
    g.add_edge("a", "b", 1)
    with pytest.raises(ValueError):
        g.shortest_path("b", "a")


def test_path_to_itself_is_zero():
    g = WeightedGraph()
    g.add_edge("home", "dog-a", 5)
    result = g.shortest_path("home", "home")
    assert result.distance == 0
    assert result.path == ["home"]


def test_multi_stop_route():
    g = WeightedGraph()
    g.add_edge("home", "dog-a", 4)
    g.add_edge("dog-a", "dog-b", 3)
    g.add_edge("home", "dog-b", 9)
    g.add_edge("dog-b", "dog-c", 2)
    result = g.shortest_path("home", "dog-c")
    assert result.distance == 9
    assert result.path == ["home", "dog-a", "dog-b", "dog-c"]


def test_unknown_node_raises_value_error():
    g = WeightedGraph()
    g.add_edge("home", "dog-a", 5)
    with pytest.raises(ValueError):
        g.shortest_path("nowhere", "dog-a")
    with pytest.raises(ValueError):
        g.shortest_path("home", "nowhere")


@pytest.mark.parametrize("weight", [-1, float("inf"), float("nan")])
def test_bad_weight_raises_value_error(weight):
    with pytest.raises(ValueError):
        WeightedGraph().add_edge("a", "b", weight)


def test_zero_weight_edge_is_allowed():
    g = WeightedGraph()
    g.add_edge("a", "b", 0)
    assert g.shortest_path("a", "b").distance == 0


def test_larger_graph_matches_known_answer():
    g = WeightedGraph()
    edges = [
        ("s", "a", 7), ("s", "b", 2), ("b", "a", 3), ("a", "c", 1),
        ("b", "c", 8), ("c", "t", 4), ("a", "t", 9), ("b", "t", 20),
    ]
    for u, v, w in edges:
        g.add_edge(u, v, w)
    result = g.shortest_path("s", "t")
    assert result.distance == 10
    assert result.path == ["s", "b", "a", "c", "t"]
