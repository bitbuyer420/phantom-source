from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from phantom import library, search

class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_libraries_survive_reload_without_cross_contamination(self):
        route = {"id": "a", "name": "Walk", "folder": "Tests", "waypoints": [{"lat": 1, "lon": 2}, {"lat": 2, "lon": 3}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(library, 'LIBRARY_FILE', Path(tmp) / 'library.json'):
            await library.put_library({"routes": [route]}, 'phone-a')
            self.assertEqual((await library.get_library('phone-a'))['routes'], [route])
            self.assertEqual((await library.get_library('phone-b'))['routes'], [])
            self.assertEqual(library.LIBRARY_FILE.stat().st_mode & 0o777, 0o600)
    def test_invalid_coordinate_and_duplicate_ids_rejected(self):
        valid = {"id": "a", "waypoints": [{"lat": 1, "lon": 2}, {"lat": 2, "lon": 3}]}
        with self.assertRaises(HTTPException): library.validate_routes([valid, valid])
        valid['waypoints'][0]['lat'] = float('nan')
        with self.assertRaises(HTTPException): library.validate_routes([valid])
    async def test_corrupt_library_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(library, 'LIBRARY_FILE', Path(tmp) / 'library.json'):
            library.LIBRARY_FILE.write_text('broken')
            with self.assertRaises(HTTPException): await library.put_library({'routes': []}, 'phone-a')
            self.assertEqual(library.LIBRARY_FILE.read_text(), 'broken')

class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_query_uses_cache(self):
        search._cache.clear()
        response = unittest.mock.Mock()
        response.json.return_value = [{'lat': '1', 'lon': '2', 'display_name': 'Test city'}]
        client = AsyncMock()
        client.get.return_value = response
        with patch.object(search, 'provider_url', return_value='https://example.org/search'), patch.object(search.httpx, 'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value = client
            first = await search.search('Test city')
            second = await search.search('test city')
            self.assertEqual(first, second)
            self.assertEqual(client.get.await_count, 1)
    async def test_bad_provider_reply_is_actionable(self):
        search._cache.clear()
        response = unittest.mock.Mock()
        response.json.return_value = {'error': 'bad response'}
        client = AsyncMock(); client.get.return_value = response
        with patch.object(search, 'provider_url', return_value='https://example.org/search'), patch.object(search.httpx, 'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(HTTPException) as raised: await search.search('Another city')
            self.assertEqual(raised.exception.status_code, 502)
            self.assertIn('coordinates', raised.exception.detail)
