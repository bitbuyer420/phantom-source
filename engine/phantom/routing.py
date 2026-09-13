"""Route generation: road-following routes (OSRM) and straight great-circle paths.

Returns polylines as lists of (lat, lon) tuples ready for `mover.iter_route_fixes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx

LatLon = tuple[float, float]

# Public OSRM demo server — fine for prototyping (non-commercial, ~1 req/sec, no SLA).
# Point Phantom at a self-hosted OSRM for real use.
DEFAULT_OSRM = "https://router.project-osrm.org"


class RouteError(RuntimeError):
    pass


@dataclass
class Route:
    points: list[LatLon]      # decoded polyline (lat, lon)
    distance_m: float | None  # OSRM-reported driving distance
    duration_s: float | None  # OSRM-reported driving duration (real-world estimate)


async def road_route(
    waypoints: Sequence[LatLon],
    *,
    profile: str = "driving",
    osrm_base: str = DEFAULT_OSRM,
    timeout: float = 30.0,
    max_snap_m: float = 5000.0,
) -> Route:
    """Fetch a road-following route through `waypoints` (>=2) from an OSRM server."""
    if profile not in {"driving", "walking", "cycling"}:
        raise RouteError("Unsupported routing profile")
    if profile != "driving" and osrm_base.rstrip("/") == DEFAULT_OSRM:
        raise RouteError(f"{profile.capitalize()} routing requires a configured provider; choose straight line or driving.")
    pts = [(float(a), float(b)) for a, b in waypoints]
    if len(pts) < 2:
        raise RouteError("road route needs at least 2 waypoints")
    coords = ";".join(f"{lon},{lat}" for lat, lon in pts)  # OSRM uses lon,lat order
    url = f"{osrm_base.rstrip('/')}/route/v1/{profile}/{coords}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise RouteError(f"routing request failed: {e}") from e

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteError(f"OSRM returned {data.get('code')}: {data.get('message', 'no route')}")

    # If a tap snapped to a road far away (ocean, Antarctica, remote spots), the
    # returned route would lurch to a distant coast — reject so callers fall back
    # to a straight-line path at the actual point instead.
    snapped = data.get("waypoints") or []
    worst = max((float(w.get("distance") or 0.0) for w in snapped), default=0.0)
    if worst > max_snap_m:
        raise RouteError(f"nearest road is {worst / 1000:.1f} km away — not a drivable spot")

    route = data["routes"][0]
    # GeoJSON coordinates are [lon, lat]; convert to (lat, lon).
    line = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
    if not line:
        raise RouteError("OSRM returned an empty geometry")
    return Route(points=line, distance_m=route.get("distance"), duration_s=route.get("duration"))


def straight_route(waypoints: Sequence[LatLon]) -> Route:
    """A direct path through the given waypoints (great-circle interpolation happens
    downstream in the mover). Used for fly mode and manual straight-line moves."""
    pts = [(float(a), float(b)) for a, b in waypoints]
    if len(pts) < 2:
        raise RouteError("straight route needs at least 2 waypoints")
    from . import geo

    return Route(points=pts, distance_m=geo.path_length_m(pts), duration_s=None)
