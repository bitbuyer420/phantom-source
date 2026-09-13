import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from phantom import server, library

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_token_and_route_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(library, 'LIBRARY_FILE', Path(tmp)/'library.json'), patch.object(server, 'EXPECTED_TOKEN', 'test-only'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()), base_url='http://127.0.0.1') as client:
                self.assertEqual((await client.get('/api/library')).status_code, 401)
                self.assertEqual((await client.get('/api/session/log')).status_code, 401)
                headers = {'X-Phantom-Token': 'test-only'}
                route = {'id':'scenario', 'name':'Test','waypoints':[{'lat':0,'lon':0,'dwell_s':3},{'lat':1,'lon':1}], 'settings':{'mph':5,'roundTrip':True}}
                response = await client.put('/api/library?udid=phone-a', headers=headers, json={'routes':[route]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((await client.get('/api/library?udid=phone-a',headers=headers)).json()['routes'], [route])
                self.assertEqual((await client.get('/api/library?udid=phone-b',headers=headers)).json()['routes'], [])
                self.assertEqual((await client.get('/api/search?q=a',headers=headers)).status_code,422)
    async def test_route_validation_happens_before_network(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()), base_url='http://127.0.0.1') as client:
            self.assertEqual((await client.get('/api/route?coords=91,0;0,0')).status_code,400)
