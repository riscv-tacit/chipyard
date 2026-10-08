#!/usr/bin/env python3
"""Elaborate real BOOM tiles through Chipyard; run after sbt chipyard/compile."""
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'tools/pmu/target/boom'
export = ROOT / 'generators/chipyard/target/streams/compile/dependencyClasspath/_global/streams/export'
if not export.exists():
    raise SystemExit('Run sbt chipyard/compile before this test.')
classpath = [str(ROOT / 'generators/chipyard/target/scala-2.13/classes')]
classpath += export.read_text().strip().split(os.pathsep)
for entry in classpath:
    if not Path(entry).exists():
        raise SystemExit(f'Missing compiled dependency: {entry}; run sbt chipyard/compile.')
env = dict(os.environ, PATH=str(ROOT / '.conda-env/bin') + os.pathsep + os.environ['PATH'])

# Separate JVMs match normal Chipyard generation and avoid global generator state.
for config, scopes in [('DualSmallBoomV3PMUConfig', 2),
                       ('MediumBoomV3PMUConfig', 1),
                       ('SmallBoomV3Config', 0)]:
    output = OUT / config
    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob(f'{config}*.pmu.json'):
        stale.unlink()
    log_path = output / 'elaboration.log'
    print(f'Elaborating {config}; log: {log_path}', flush=True)
    with log_path.open('w') as log:
        result = subprocess.run([
            str(ROOT / '.conda-env/bin/java'), '-Xmx12G', '-XX:ActiveProcessorCount=4',
            '-cp', os.pathsep.join(classpath), 'chipyard.Generator',
            '--target-dir', str(output), '--name', config,
            '--top-module', 'chipyard.harness.TestHarness', '--legacy-configs', f'chipyard:{config}'
        ], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f'{config} failed (exit {result.returncode}); see {log_path}')
    annotations = json.loads((output / f'{config}.anno.json').read_text())
    assert not any('pmu' in a.get('class', '').lower() for a in annotations), 'PMU annotations reached the output'
    circuit = (output / f'{config}.fir').read_text()
    banks = re.findall(r'\binst \w+ of TilePMU(?:_\d+)?\b', circuit)
    assert len(banks) == scopes, (config, len(banks))
    dts = (output / f'{config}.dts').read_text()
    assert dts.count('"ucbbar,pmu-v1"') == scopes, 'Unexpected number of PMU MMIO devices'
    for tile in range(scopes):
        base = 0x40000000 + tile * 0x1000
        node = re.search(rf'pmu@{base:x} \{{(.*?)\}};', dts, re.S)
        assert node, f'Missing PMU node at {base:#x}'
        assert f'pmu,source = "boom_tile_{tile}"' in node.group(1)
        assert f'reg = <0x{base:x} 0x1000>' in node.group(1)
        assert re.search(r'pmu,counter-bits = <(0x40|64)>', node.group(1)), node.group(1)
        paths = list(output.glob(f'{config}.0x{base:x}.*.regmap.json'))
        assert len(paths) == 1, paths
        peripheral = json.loads(paths[0].read_text())['peripheral']
        assert int(peripheral['baseAddress'], 16) == base
        fields = {key: value for entry in peripheral['regfields'] for key, value in entry.items()}
        def location(field):
            return int(field['byteOffset'], 16) * 8 + field['bitOffset']
        for name, bit, width, access in [('enable', 0, 1, 'RW'), ('command', 0x08 * 8, 2, 'W'),
                                       ('status', 0x10 * 8, 2, 'R'), ('version', 0x18 * 8, 16, 'R'),
                                       ('count', 0x18 * 8 + 16, 16, 'R'), ('width', 0x18 * 8 + 32, 16, 'R')]:
            assert (location(fields[name]), fields[name]['bitWidth'], fields[name]['accessType']) == (bit, width, access)
        for event in range(19):
            for suffix, displacement in [('lo', 0), ('hi', 4)]:
                field = fields[f'counter_{event}_{suffix}']
                assert location(field) == (0x100 + event * 8 + displacement) * 8
                assert field['bitWidth'] == 32 and field['accessType'] == 'R'
    manifests = sorted(output.glob(f'{config}.*.pmu.json'))
    assert [m.name for m in manifests] == [f'{config}.boom_tile_{i}.pmu.json' for i in range(scopes)], manifests
    if scopes:
        catalogs = [json.loads(m.read_text()) for m in manifests]
        assert [m['source'] for m in catalogs] == [f'boom_tile_{i}' for i in range(scopes)]
        for catalog in catalogs:
            assert set(catalog) == {'schemaVersion', 'source', 'events'} and catalog['schemaVersion'] == 2
            expected = {
                'rob.instructions_retired': 'instructions',
                'pipeline.dispatched_uops': 'uops',
                'branch.mispredict_redirects': 'redirects',
                **{name: 'requests' for name in [
                    'frontend.icache_line_misses', 'frontend.itlb_walk_requests',
                    'lsu.dcache_line_misses', 'lsu.dtlb_walk_requests']},
                **{name: 'cycles' for name in [
                    'tile.cycles', 'pipeline.frontend_starved_cycles',
                    'pipeline.backend_blocked_cycles', 'pipeline.redirect_blocked_cycles',
                    'pipeline.rob_blocked_cycles', 'pipeline.rename_blocked_cycles',
                    'pipeline.issue_queue_blocked_cycles', 'lsu.ldq_blocked_cycles',
                    'lsu.stq_blocked_cycles', 'pipeline.serialization_blocked_cycles',
                    'rob.head_load_wait_cycles', 'rob.nonempty_no_retire_cycles']}}
            assert len(catalog['events']) == 19
            assert [e['id'] for e in catalog['events']] == list(range(19))
            assert [e['name'] for e in catalog['events']] == sorted(expected), 'Event IDs are not in name order'
            actual = {e['name']: e['unit'] for e in catalog['events']}
            assert actual == expected, (config, actual)
            assert all(e['description'] for e in catalog['events'])
        assert all(c['events'] == catalogs[0]['events'] for c in catalogs), 'Per-tile event lists differ'
    print(f'PASS: {config}: {scopes} tile PMUs, per-tile event lists, MMIO maps, no PMU annotations', flush=True)
