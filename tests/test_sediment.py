"""The retired HTTP endpoint leaves instance files untouched."""
import builtins
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from ember.instances import InstanceRegistry
from ember.health_check import check_memory


class SedimentEvidenceTests(unittest.TestCase):
    def test_retired_endpoint_does_not_read_or_write_ledger(self):
        import sidecar

        with tempfile.TemporaryDirectory() as directory:
            registry = InstanceRegistry(directory, ['default'])
            root = Path(registry.dir_of('default'))
            root.mkdir(parents=True)
            ledger = root / 'sediment.jsonl'
            original_open = builtins.open
            original_io_open = io.open

            def guard(opener):
                def checked(file, *args, **kwargs):
                    if not isinstance(file, int) and Path(file).name == 'sediment.jsonl':
                        raise AssertionError('Retired ledger must not be accessed')
                    return opener(file, *args, **kwargs)
                return checked

            def snapshot():
                return {str(p.relative_to(root)): p.read_bytes()
                        for p in root.rglob('*') if p.is_file()}

            for existing in (False, True):
                with self.subTest(existing_ledger=existing):
                    if existing:
                        ledger.write_bytes(b'{"version":1,"evidence_event_ids":["EVT-old"]}\n')
                    registry._services.clear()
                    before = snapshot()
                    with patch.object(sidecar, 'REGISTRY', registry), \
                            patch.object(sidecar, 'TOKEN', ''), \
                            patch('builtins.open', side_effect=guard(original_open)), \
                            patch('io.open', side_effect=guard(original_io_open)):
                        client = TestClient(sidecar.app)
                        try:
                            for body in ('{"version":2,"evidence_event_ids":["EVT-new"]}',
                                         '{}', 'invalid json'):
                                response = client.post('/ember/sleep/sediment/evidence',
                                                       content=body)
                                self.assertEqual(response.status_code, 410)
                                self.assertEqual(response.json(), {
                                    'status': 'retired',
                                    'code': 'legacy_sediment_evidence_retired',
                                    'hint': 'Sediment is now an App-only letter with no evidence ledger.',
                                })
                        finally:
                            client.close()
                        check_memory(root)
                    self.assertEqual(snapshot(), before)
                    with patch('builtins.open', side_effect=guard(original_open)), \
                            patch('io.open', side_effect=guard(original_io_open)):
                        registry.service('default')
