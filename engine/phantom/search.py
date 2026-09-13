"""Explicit-submit geocoding with bounded caching and provider configuration.

Public Nominatim is for modest personal use only. Distributed deployments must
configure a Nominatim-compatible provider whose capacity covers all users.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
_lock = asyncio.Lock()
_cache: OrderedDict[tuple[str, str], tuple[float, list]] = OrderedDict()
_last_request = 0.0
CACHE_SECONDS = 3600


def provider_url() -> str:
    from .server import _read_config

    url = os.environ.get("PHANTOM_GEOCODER_URL") or _read_config().get("geocoder_url")
    url = str(url or "https://nominatim.openstreetmap.org/search").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(503, "Configure an HTTPS geocoding endpoint without embedded credentials.")
    return url


@router.get("/api/search")
async def search(q: str = Query(..., min_length=3, max_length=200)) -> list:
    global _last_request
    query = " ".join(q.split())
    if len(query) < 3:
        raise HTTPException(422, "Enter at least three characters.")
    url = provider_url()
    key = (url, query.casefold())
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            _cache.move_to_end(key)
            return cached[1]
        delay = max(0.0, 1.1 - (time.monotonic() - _last_request))
        if delay:
            await asyncio.sleep(delay)
        _last_request = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.get(
                    url,
                    params={"q": query, "format": "json", "limit": 6},
                    headers={"User-Agent": "Phantom/1.1 (desktop location testing; explicit search)",
                             "Accept-Language": "en"},
                )
                response.raise_for_status()
                results = response.json()
                if not isinstance(results, list):
                    raise ValueError("Expected a list of places")
                results = [
                    {"lat": item["lat"], "lon": item["lon"], "display_name": str(item["display_name"])}
                    for item in results[:6]
                ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, "Place search is unavailable. Retry, or enter coordinates directly.") from exc
        _cache[key] = (time.monotonic(), results)
        _cache.move_to_end(key)
        while len(_cache) > 200:
            _cache.popitem(last=False)
        return results
