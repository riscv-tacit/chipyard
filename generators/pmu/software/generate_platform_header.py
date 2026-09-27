#!/usr/bin/env python3
"""Join Chipyard's finalized PMU catalog and JSON device tree into a C header."""
import argparse
import json
import re
from pathlib import Path


def generate(manifests, platform, source):
    catalogs = [m for m in manifests if m['source'] == source]
    if len(catalogs) != 1:
        raise ValueError(f'expected exactly one catalog for {source}')
    catalog = catalogs[0]
    if catalog['schemaVersion'] != 1 or not catalog['finalized']:
        raise ValueError('a finalized schema-v1 PMU manifest is required')
    if catalog['counterBits'] != 64:
        raise ValueError('this platform interface requires 64-bit counters')
    events = catalog['events']
    if not events or [e['id'] for e in events] != list(range(len(events))):
        raise ValueError('counter IDs must be dense and ordered')
    names = [e['name'] for e in events]
    symbols = [re.sub(r'[^A-Za-z0-9_]', '_', name).upper() for name in names]
    if len(set(symbols)) != len(symbols):
        raise ValueError('event names collide after conversion to C identifiers')
    if any(not name or not isinstance(e['incrementBits'], int) or
           not 1 <= e['incrementBits'] <= 64 for name, e in zip(names, events)):
        raise ValueError('invalid event name or increment width')
    # Chipyard ResourceBinding JSON represents addresses as base/size objects;
    # this is not a generic flattened-device-tree parser or bus translator.
    soc = platform['soc']
    if soc.get('ranges') != []:
        raise ValueError('expected a directly mapped Chipyard soc bus')
    devices = [d for d in soc.values() if isinstance(d, dict) and
               d.get('pmu,source') == [source] and
               'ucbbar,pmu-v1' in d.get('compatible', [])]
    if len(devices) != 1 or len(devices[0]['reg']) != 1:
        raise ValueError(f'expected exactly one native MMIO PMU for {source}')
    region = devices[0]['reg'][0]
    base = region['base']
    if base < 0 or base % 4096 or region['size'] != 4096 or 0x100 + 8 * len(events) > 4096:
        raise ValueError('invalid PMU MMIO aperture or counter count')
    lines = [
        '/* Generated from this elaboration; do not edit. */',
        '#ifndef TILE_PMU_PLATFORM_H', '#define TILE_PMU_PLATFORM_H',
        '#include "tile_pmu.h"',
        f'#define TILE_PMU_SOURCE {json.dumps(source)}',
        f'#define TILE_PMU_BASE ((uintptr_t)UINT64_C(0x{base:x}))',
        f'#define TILE_PMU_COUNTER_COUNT {len(events)}u',
        f'#define TILE_PMU_COUNTER_BITS {catalog["counterBits"]}u',
        '', '/* X(symbol, id, key, unit) */', '#define TILE_PMU_EVENTS(X) \\',
    ]
    for i, (event, symbol) in enumerate(zip(events, symbols)):
        suffix = ' \\' if i + 1 < len(events) else ''
        lines.append(f'  X({symbol}, {event["id"]}, {json.dumps(event["name"])}, '
                     f'{json.dumps(event["unit"])}){suffix}')
    lines += [
        '', '#define TILE_PMU_ENUM(symbol, id, key, unit) TILE_PMU_EVENT_##symbol = id,',
        'enum tile_pmu_event_id { TILE_PMU_EVENTS(TILE_PMU_ENUM) };',
        '#undef TILE_PMU_ENUM', '', '#endif', '',
    ]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        header = generate(json.loads(args.manifest.read_text()),
                          json.loads(args.platform.read_text()), args.source)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(header)


if __name__ == '__main__':
    main()
