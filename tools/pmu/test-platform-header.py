#!/usr/bin/env python3
"""Check device matching and rejection of invalid generated PMU platform headers."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'platform_header', ROOT / 'generators/pmu/software/generate_platform_header.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformHeaderTest(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(schemaVersion=2, source='tile1', events=[
            dict(id=0, name='rob.retired', unit='instructions', description=''),
            dict(id=1, name='tile.cycles', unit='cycles', description='')])
        self.platform = dict(soc={'ranges': [],
            'pmu@40000000': {
                'compatible': ['ucbbar,pmu-v1'], 'pmu,source': ['tile0'], 'pmu,counter-bits': [48],
                'reg': [dict(base=0x40000000, size=4096)]},
            'pmu@40001000': {
                'compatible': ['ucbbar,pmu-v1'], 'pmu,source': ['tile1'], 'pmu,counter-bits': [64],
                'reg': [dict(base=0x40001000, size=4096)]}})

    def emit(self):
        return module.generate(self.manifest, self.platform)

    def test_match_source_and_preserve_ids(self):
        header = self.emit()
        self.assertIn('#define TILE_PMU_SOURCE "tile1"', header)
        self.assertIn('UINT64_C(0x40001000)', header)
        self.assertIn('#define TILE_PMU_COUNTER_COUNT 2u', header)
        self.assertIn('#define TILE_PMU_COUNTER_BITS 64u', header)
        self.assertIn('X(ROB_RETIRED, 0, "rob.retired", "instructions")', header)
        self.assertIn('X(TILE_CYCLES, 1, "tile.cycles", "cycles")', header)

    def test_counter_bits_from_device_tree(self):
        self.manifest['source'] = 'tile0'
        self.assertIn('#define TILE_PMU_COUNTER_BITS 48u', self.emit())

    def test_source_must_have_tile_pmu(self):
        self.manifest['source'] = 'tile9'
        with self.assertRaisesRegex(ValueError, 'exactly one tile PMU'):
            self.emit()

    def test_schema_version(self):
        self.manifest['schemaVersion'] = 1
        with self.assertRaisesRegex(ValueError, 'schema-v2'):
            self.emit()

    def test_counter_bits_required(self):
        del self.platform['soc']['pmu@40001000']['pmu,counter-bits']
        with self.assertRaisesRegex(ValueError, 'pmu,counter-bits'):
            self.emit()

    def test_counter_bits_range(self):
        self.platform['soc']['pmu@40001000']['pmu,counter-bits'] = [65]
        with self.assertRaisesRegex(ValueError, 'pmu,counter-bits'):
            self.emit()

    def test_identifier_collision(self):
        self.manifest['events'][1]['name'] = 'ROB_RETIRED'
        with self.assertRaisesRegex(ValueError, 'collide'):
            self.emit()

    def test_bad_ids(self):
        self.manifest['events'][1]['id'] = 3
        with self.assertRaisesRegex(ValueError, 'positions'):
            self.emit()

    def test_bad_region(self):
        self.platform['soc']['pmu@40001000']['reg'][0]['size'] = 128
        with self.assertRaisesRegex(ValueError, 'region'):
            self.emit()

    def test_no_implicit_bus_translation(self):
        self.platform['soc']['ranges'] = [dict(base=0x1000)]
        with self.assertRaisesRegex(ValueError, 'directly mapped'):
            self.emit()


if __name__ == '__main__':
    unittest.main()
