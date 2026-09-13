"""Atomic, device-scoped route/scenario library independent of the GUI origin."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()
LIBRARY_FILE = Path.home() / ".config" / "phantom" / "library.json"


def read_library() -> dict:
    try:
        value = json.loads(LIBRARY_FILE.read_text())
        if not isinstance(value, dict):
            raise ValueError("Invalid library root")
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise HTTPException(500, "Route library could not be read. Existing data has been preserved.") from exc


def validate_routes(routes: object) -> list:
    if not isinstance(routes, list) or len(routes) > 100:
        raise HTTPException(422, "A device library supports up to 100 routes.")
    try:
        if len(json.dumps(routes, allow_nan=False)) > 1_000_000:
            raise ValueError("Library is too large")
        ids = set()
        for route in routes:
            if not isinstance(route, dict):
                raise ValueError("Invalid route")
            identifier = route.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 100 or identifier in ids:
                raise ValueError("Routes need unique IDs")
            ids.add(identifier)
            for key in ("name", "folder"):
                if not isinstance(route.get(key, ""), str) or len(route.get(key, "")) > 120:
                    raise ValueError("Route names and folders must be 120 characters or fewer")
            points = route.get("waypoints")
            if not isinstance(points, list) or not 2 <= len(points) <= 50:
                raise ValueError("Routes need 2–50 waypoints")
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError("Invalid waypoint")
                lat, lon = float(point["lat"]), float(point["lon"])
                if not math.isfinite(lat) or not math.isfinite(lon) or abs(lat) > 90 or abs(lon) > 180:
                    raise ValueError("Waypoint coordinates are outside their valid range")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return routes


@router.get("/api/library")
async def get_library(udid: str = Query(default="local", max_length=200)) -> dict:
    return {"routes": read_library().get(udid or "local", [])}


@router.put("/api/library")
async def put_library(payload: dict = Body(...), udid: str = Query(default="local", max_length=200)) -> dict:
    routes = validate_routes(payload.get("routes"))
    data = read_library()
    data[udid or "local"] = routes
    LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="library-", suffix=".tmp", dir=LIBRARY_FILE.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, LIBRARY_FILE)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    return {"routes": routes}
