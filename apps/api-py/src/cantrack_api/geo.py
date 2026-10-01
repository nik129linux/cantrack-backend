"""Great-circle distance for the S2 pickup plan (no external geo library).

One function, ``haversine_km``, on a spherical earth with the IUGG mean radius
(6371.0088 km). The plan uses it for nearest-neighbour ordering, walking legs
at 5 km/h and the group-compatibility radius. Pure maths: O(1) per call, no
data structures involved.
"""

import math

#: IUGG mean earth radius in kilometres (WGS-84 derived).
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance between two WGS-84 points, in km.

    The haversine formula is numerically well behaved for short distances
    (the plan's neighbourhood legs) and symmetric: the same pair in either
    order gives the identical float. Antimeridian and pole inputs are safe —
    longitudes only ever enter through ``sin(delta/2)**2``.

    Big-O: O(1).

    Args:
        lat1: Latitude of the first point, degrees.
        lng1: Longitude of the first point, degrees.
        lat2: Latitude of the second point, degrees.
        lng2: Longitude of the second point, degrees.

    Returns:
        The great-circle distance in kilometres (0.0 for identical points).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
