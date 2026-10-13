import pytest

from cantrack_api.structures import Quadtree


def make_tree(**overrides):
    return Quadtree(0.0, 0.0, 10_000.0, 10_000.0, **overrides)


class TestQuadtree:
    def test_empty_query_returns_nothing(self):
        nearby_walkers = make_tree()
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == []
        assert nearby_walkers.size() == 0

    def test_finds_only_points_inside_radius_nearest_first(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker-near", 5_100, 5_000)
        nearby_walkers.insert("walker-mid", 5_000, 6_500)
        nearby_walkers.insert("walker-far", 9_900, 9_900)
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == ["walker-near", "walker-mid"]

    def test_point_exactly_on_radius_is_included(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("edge", 7_000, 5_000)
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == ["edge"]

    def test_splitting_keeps_every_point_findable(self):
        nearby_walkers = make_tree(node_capacity=2)
        for index in range(50):
            nearby_walkers.insert(f"walker-{index}", 100.0 * index, 100.0 * index)
        found = nearby_walkers.query_radius(0, 0, 20_000)
        assert sorted(found) == sorted(f"walker-{index}" for index in range(50))
        assert nearby_walkers.size() == 50

    def test_max_depth_caps_identical_points(self):
        nearby_walkers = make_tree(node_capacity=1, max_depth=3)
        for index in range(30):
            nearby_walkers.insert(f"same-spot-{index}", 2_500, 2_500)
        assert len(nearby_walkers.query_radius(2_500, 2_500, 1)) == 30

    def test_reinsert_moves_the_walker(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker", 1_000, 1_000)
        nearby_walkers.insert("walker", 9_000, 9_000)
        assert nearby_walkers.query_radius(1_000, 1_000, 500) == []
        assert nearby_walkers.query_radius(9_000, 9_000, 500) == ["walker"]
        assert nearby_walkers.size() == 1

    def test_remove(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker", 1_000, 1_000)
        assert nearby_walkers.remove("walker") is True
        assert nearby_walkers.remove("walker") is False
        assert nearby_walkers.query_radius(1_000, 1_000, 500) == []

    def test_corners_are_inside_bounds(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("min-corner", 0.0, 0.0)
        nearby_walkers.insert("max-corner", 10_000.0, 10_000.0)
        assert nearby_walkers.size() == 2

    @pytest.mark.parametrize("east,north", [(-1, 5), (5, -1), (10_001, 5), (5, 10_001)])
    def test_point_outside_bounds_is_rejected(self, east, north):
        with pytest.raises(ValueError):
            make_tree().insert("lost", east, north)

    def test_negative_radius_is_rejected(self):
        with pytest.raises(ValueError):
            make_tree().query_radius(0, 0, -1)
