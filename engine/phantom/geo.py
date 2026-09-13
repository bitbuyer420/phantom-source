"""Pure geodesic math for Phantom. No external dependencies.

Coordinates are (latitude, longitude) tuples in decimal degrees throughout.
Great-circle interpolation is used everywhere so the same code is accurate at
both street scale (drive) and continental scale (fly).
"""

from __future__ import annotations

import math

# Mean Earth radius (IUGG), metres.
EARTH_RADIUS_M = 6_371_008.8

# Default speed presets in metres/second.
#   walk  ~5 km/h,  cycle ~18 km/h,  drive ~60 km/h,  fly ~800 km/h (cruise).
SPEED_PRESETS_MPS: dict[str, float] = {
    "walk": 1.4,
    "cycle": 5.0,
    "drive": 16.7,
    "fly": 222.0,
}


def _rad(deg: float) -> float:
    return math.radians(deg)


def _deg(rad: float) -> float:
    return math.degrees(rad)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    p1, p2 = _rad(lat1), _rad(lat2)
    dp = _rad(lat2 - lat1)
    dl = _rad(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (compass heading, 0-360) from point 1 to point 2."""
    p1, p2 = _rad(lat1), _rad(lat2)
    dl = _rad(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (_deg(math.atan2(y, x)) + 360.0) % 360.0


def gc_interpolate(
    lat1: float, lon1: float, lat2: float, lon2: float, fraction: float
) -> tuple[float, float]:
    """Point at `fraction` (0..1) of the great circle from point 1 to point 2."""
    if fraction <= 0.0:
        return lat1, lon1
    if fraction >= 1.0:
        return lat2, lon2
    p1, l1, p2, l2 = _rad(lat1), _rad(lon1), _rad(lat2), _rad(lon2)
    dp = p2 - p1
    dl = l2 - l1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * math.asin(min(1.0, math.sqrt(a)))
    if d == 0:
        return lat1, lon1
    A = math.sin((1 - fraction) * d) / math.sin(d)
    B = math.sin(fraction * d) / math.sin(d)
    x = A * math.cos(p1) * math.cos(l1) + B * math.cos(p2) * math.cos(l2)
    y = A * math.cos(p1) * math.sin(l1) + B * math.cos(p2) * math.sin(l2)
    z = A * math.sin(p1) + B * math.sin(p2)
    lat = math.atan2(z, math.hypot(x, y))
    lon = math.atan2(y, x)
    return _deg(lat), _deg(lon)


def destination(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    """Point reached by travelling `dist_m` metres along `bearing` from a start point."""
    p1 = _rad(lat)
    l1 = _rad(lon)
    br = _rad(bearing)
    dr = dist_m / EARTH_RADIUS_M
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(dr) * math.cos(p1),
        math.cos(dr) - math.sin(p1) * math.sin(p2),
    )
    # Normalise longitude to [-180, 180).
    return _deg(p2), (_deg(l2) + 540) % 360 - 180


def path_length_m(points: list[tuple[float, float]]) -> float:
    """Total great-circle length of a polyline."""
    return sum(
        haversine_m(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )
