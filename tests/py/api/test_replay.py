import pytest

from cantrack_api.replay import GpsPoint, build_timeline, find_gps_at, read_timeline


def make_rows():
    return [
        {"kind": "check_out", "at": "2026-10-05T12:00:00+00:00", "payload": {}},
        {"kind": "check_in", "at": "2026-10-05T10:00:00+00:00", "payload": {}},
        {"kind": "photo", "at": "2026-10-05T11:00:00+00:00", "payload": {"n": 1}},
    ]


class TestReplay:
    def test_build_sorts_by_time(self):
        assert [event.kind for event in build_timeline(make_rows()).to_array()] == ["check_in", "photo", "check_out"]

    def test_read_forward_and_backward(self):
        walk_timeline = build_timeline(make_rows())
        assert [event.kind for event in read_timeline(walk_timeline, 0, 2, "forward")] == ["check_in", "photo"]
        assert [event.kind for event in read_timeline(walk_timeline, 2, 2, "backward")] == ["check_out", "photo"]

    def test_bad_direction_is_rejected(self):
        with pytest.raises(ValueError):
            read_timeline(build_timeline(make_rows()), 0, 1, "sideways")

    def test_start_out_of_range_is_rejected(self):
        with pytest.raises(IndexError):
            read_timeline(build_timeline(make_rows()), 9, 1, "forward")

    def test_gps_exact_hit(self):
        sorted_points = [GpsPoint(1000.0, 6.2, -75.5), GpsPoint(2000.0, 6.3, -75.6)]
        assert find_gps_at(sorted_points, 2000.0) == GpsPoint(2000.0, 6.3, -75.6)

    def test_gps_between_points_returns_the_earlier_one(self):
        sorted_points = [GpsPoint(1000.0, 6.2, -75.5), GpsPoint(2000.0, 6.3, -75.6)]
        assert find_gps_at(sorted_points, 1500.0) == GpsPoint(1000.0, 6.2, -75.5)

    def test_gps_before_first_is_rejected(self):
        with pytest.raises(IndexError):
            find_gps_at([GpsPoint(1000.0, 6.2, -75.5)], 999.0)

    def test_gps_on_empty_is_rejected(self):
        with pytest.raises(ValueError):
            find_gps_at([], 1000.0)
