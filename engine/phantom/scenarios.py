"""Deterministic, bounded geofence observations for repeatable route tests."""
from __future__ import annotations

import math
from .geo import haversine_m


class Geofences:
    """Report membership transitions at confirmed device fixes.

    These are sampled observations, not a promise about notifications emitted by
    an iOS app. A route can pass through a small fence between device updates.
    """

    def __init__(self, fences=None):
        fences = [] if fences is None else fences
        if not isinstance(fences, list) or len(fences) > 50:
            raise ValueError("Use at most 50 geofences")
        self.fences = []
        self.inside = {}
        for index, fence in enumerate(fences):
            if not isinstance(fence, dict):
                raise ValueError("Invalid geofence")
            lat, lon, radius = (float(fence[key]) for key in ("lat", "lon", "radius_m"))
            if not all(math.isfinite(value) for value in (lat, lon, radius)) or abs(lat) > 90 or abs(lon) > 180 or not 1 <= radius <= 100000:
                raise ValueError("Geofence needs valid coordinates and a radius of 1–100,000 metres")
            self.fences.append({"id": index, "name": str(fence.get("name") or f"Waypoint {index + 1}")[:120],
                                "lat": lat, "lon": lon, "radius_m": radius})

    def observe(self, lat: float, lon: float) -> list[dict]:
        events = []
        for fence in self.fences:
            current = haversine_m(lat, lon, fence["lat"], fence["lon"]) <= fence["radius_m"]
            prior = self.inside.get(fence["id"], False)
            if prior != current:
                events.append({"type": "geofence", "name": fence["name"],
                               "transition": "entered" if current else "exited", "lat": lat, "lon": lon})
            self.inside[fence["id"]] = current
        return events
