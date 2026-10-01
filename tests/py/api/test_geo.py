"""S2: haversine great-circle distance in km — `cantrack_api.geo.haversine_km`.

Signature pinned: haversine_km(lat1, lng1, lat2, lng2) -> float (kilometres,
WGS-84-ish mean earth radius; exact radius is an implementation choice, hence
the tolerances). Used by the pickup plan: nearest-neighbour ordering and
walking legs at 5 km/h. Geo maths is asserted with tolerances (the brief
allows it); everything else in S2 stays exact.
"""

import pytest

from cantrack_api.geo import haversine_km


class TestKnownDistances:
    def test_one_degree_of_latitude_is_about_111_19_km(self):
        assert haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.19, abs=0.5)

    def test_medellin_to_bogota_is_about_240_km(self):
        # Plaza de la Candelaria (Medellin) -> Plaza de Bolivar (Bogota)
        distance = haversine_km(6.2476, -75.5658, 4.7110, -74.0721)
        assert distance == pytest.approx(240.0, abs=20.0)

    def test_equator_quarter_turn_is_about_10_007_km(self):
        # A quarter of a great circle on the MEAN radius (the one the pole
        # test pins): 6371.0088 km * pi/2 ~ 10007.5 km. The previous 10018
        # was a quarter of the EQUATORIAL circumference (2*pi*6378.137/4),
        # which no single radius satisfies together with the pole test.
        assert haversine_km(0.0, 0.0, 0.0, 90.0) == pytest.approx(10007.5, abs=10.0)

    def test_pole_to_pole_is_half_the_circumference(self):
        assert haversine_km(90.0, 0.0, -90.0, 0.0) == pytest.approx(20015.0, abs=20.0)


class TestProperties:
    @pytest.mark.parametrize(
        "a,b",
        [
            ((6.2476, -75.5658), (4.7110, -74.0721)),
            ((0.0, 0.0), (1.0, 0.0)),
            ((-33.8688, 151.2093), (51.5074, -0.1278)),
            ((0.0, 179.9), (0.0, -179.9)),
        ],
    )
    def test_symmetry(self, a, b):
        assert haversine_km(a[0], a[1], b[0], b[1]) == haversine_km(b[0], b[1], a[0], a[1])

    def test_zero_distance_to_itself(self):
        assert haversine_km(6.2476, -75.5658, 6.2476, -75.5658) == 0.0

    def test_same_pole_different_longitude_is_zero(self):
        assert haversine_km(90.0, 12.0, 90.0, -160.0) < 1e-6

    def test_antimeridian_is_short_not_around_the_world(self):
        distance = haversine_km(0.0, 179.9, 0.0, -179.9)
        assert distance == pytest.approx(22.24, abs=0.5)
        assert distance < 100.0

    def test_does_not_crash_on_poles_and_extremes(self):
        assert haversine_km(-90.0, -180.0, 90.0, 180.0) == pytest.approx(20015.0, abs=20.0)
        assert haversine_km(0.0, -180.0, 0.0, 180.0) < 1e-6
