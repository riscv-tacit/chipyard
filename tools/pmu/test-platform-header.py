#!/usr/bin/env python3
"""Check source selection and reject ambiguous/invalid generated PMU bindings."""
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
        self.catalog = dict(schemaVersion=1, source='tile1', counterBits=64,
                            finalized=True, events=[
            dict(id=0, name='rob.retired', unit='instructions', incrementBits=3),
            dict(id=1, name='tile.cycles', unit='cycles', incrementBits=1)])
        self.platform = dict(soc={'ranges': [], 'pmu@40001000': {
            'compatible': ['ucbbar,pmu-v1'], 'pmu,source': ['tile1'],
            'reg': [dict(base=0x40001000, size=4096)]}})

    def emit(self, catalogs=None):
        return module.generate(catalogs or [self.catalog], self.platform, 'tile1')

    def test_select_source_and_preserve_ids(self):
        other = copy.deepcopy(self.catalog)
        other['source'] = 'tile0'
        header = self.emit([other, self.catalog])
        self.assertIn('UINT64_C(0x40001000)', header)
        self.assertIn('X(ROB_RETIRED, 0, "rob.retired", "instructions")', header)
        self.assertIn('X(TILE_CYCLES, 1, "tile.cycles", "cycles")', header)

    def test_source_must_have_native_mmio(self):
        self.platform['soc']['pmu@40001000']['pmu,source'] = ['tile0']
        with self.assertRaisesRegex(ValueError, 'native MMIO'):
            self.emit()

    def test_duplicate_catalog(self):
        with self.assertRaisesRegex(ValueError, 'exactly one catalog'):
            self.emit([self.catalog, self.catalog])

    def test_finalized_required(self):
        self.catalog['finalized'] = False
        with self.assertRaisesRegex(ValueError, 'finalized'):
            self.emit()

    def test_identifier_collision(self):
        self.catalog['events'][1]['name'] = 'ROB_RETIRED'
        with self.assertRaisesRegex(ValueError, 'collide'):
            self.emit()

    def test_bad_ids(self):
        self.catalog['events'][1]['id'] = 3
        with self.assertRaisesRegex(ValueError, 'dense and ordered'):
            self.emit()

    def test_bad_aperture(self):
        self.platform['soc']['pmu@40001000']['reg'][0]['size'] = 128
        with self.assertRaisesRegex(ValueError, 'aperture'):
            self.emit()

    def test_no_implicit_bus_translation(self):
        self.platform['soc']['ranges'] = [dict(base=0x1000)]
        with self.assertRaisesRegex(ValueError, 'directly mapped'):
            self.emit()


if __name__ == '__main__':
    unittest.main()
