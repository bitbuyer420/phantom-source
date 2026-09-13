import unittest
from phantom.scenarios import Geofences

class GeofenceTests(unittest.TestCase):
    def test_entry_exit_reported_once_per_transition(self):
        fences = Geofences([{'name':'Stop', 'lat':0,'lon':0,'radius_m':100}])
        self.assertEqual(fences.observe(1,1), [])
        self.assertEqual(fences.observe(0,0)[0]['transition'], 'entered')
        self.assertEqual(fences.observe(0,0), [])
        self.assertEqual(fences.observe(1,1)[0]['transition'], 'exited')
    def test_invalid_radius_rejected(self):
        for radius in (-1, float('nan'), float('inf'), 100001):
            with self.assertRaises(ValueError): Geofences([{'lat':0,'lon':0,'radius_m':radius}])
