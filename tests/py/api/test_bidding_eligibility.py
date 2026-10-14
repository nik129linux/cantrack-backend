from cantrack_api.bidding_eligibility import (
    is_busy_eligible, is_free_in_window, is_pack_compatible,
    nearby_walker_ids, project_to_metres,
)
from cantrack_api.geo import haversine_km
from cantrack_api.structures import IntervalTree, Quadtree, UnionFind, WeightedGraph


def make_graph():
    street_graph = WeightedGraph()
    street_graph.add_edge("home", "pickup-near", 5.0)
    street_graph.add_edge("home", "pickup-far", 20.0)
    return street_graph


class TestEligibility:
    def test_projection_matches_haversine(self):
        import pytest

        east, north = project_to_metres(6.2, -75.5, 6.2)
        near_east, near_north = project_to_metres(6.201, -75.5, 6.2)
        planar_km = ((near_east - east) ** 2 + (near_north - north) ** 2) ** 0.5 / 1000.0
        assert planar_km == pytest.approx(haversine_km(6.2, -75.5, 6.201, -75.5), abs=0.01)

    def test_free_walker_has_no_overlap(self):
        busy_windows: IntervalTree[str] = IntervalTree()
        busy_windows.insert("2026-10-05T08:00:00+00:00", "2026-10-05T09:00:00+00:00", "morning")
        assert is_free_in_window(busy_windows, "2026-10-05T10:00:00+00:00", "2026-10-05T11:00:00+00:00") is True
        assert is_free_in_window(busy_windows, "2026-10-05T08:30:00+00:00", "2026-10-05T09:30:00+00:00") is False

    def test_touching_windows_do_not_overlap(self):
        busy_windows: IntervalTree[str] = IntervalTree()
        busy_windows.insert("2026-10-05T08:00:00+00:00", "2026-10-05T09:00:00+00:00", "morning")
        assert is_free_in_window(busy_windows, "2026-10-05T09:00:00+00:00", "2026-10-05T10:00:00+00:00") is True

    def test_pack_needs_every_current_dog(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a", "old-b"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_pack_compatible(pack, "new", ["old-a"]) is True
        assert is_pack_compatible(pack, "new", ["old-a", "old-b"]) is False
        assert is_pack_compatible(pack, "new", []) is True

    def test_busy_walker_inside_detour_is_eligible(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-near", detour_limit_min=10.0,
                               dog_count=1, capacity=4) is True

    def test_busy_walker_outside_detour_is_not(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-far", detour_limit_min=10.0,
                               dog_count=1, capacity=4) is False

    def test_full_capacity_is_not_eligible(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-near", detour_limit_min=10.0,
                               dog_count=4, capacity=4) is False

    def test_nearby_query_finds_only_walkers_inside_radius(self):
        nearby_walkers: Quadtree[str] = Quadtree(-20_000_000.0, -20_000_000.0, 20_000_000.0, 20_000_000.0)
        for walker_id, lat, lng in (("walker-near", 6.201, -75.5), ("walker-far", 6.5, -75.5)):
            east, north = project_to_metres(lat, lng, 6.2)
            nearby_walkers.insert(walker_id, east, north)
        assert nearby_walker_ids(nearby_walkers, 6.2, -75.5, 2.0) == ["walker-near"]
