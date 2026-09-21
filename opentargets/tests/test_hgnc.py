"""HGNC acquisition must not silently replace the release's gene crosswalk."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opentargets import download_sources, hgnc, pipeline, transform_mechanisms
from opentargets.common import IngestError


class HgncPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = b'hgnc_id\tsymbol\tensembl_gene_id\nHGNC:1\tTEST\tENSG1\n'
        self.pin = {
            'url': 'https://example.org/hgnc-2026-07-07.txt',
            'snapshot': '2026-07-07',
            'filename': hgnc.HGNC_FILENAME,
            'bytes': len(self.data),
            'sha256': hashlib.sha256(self.data).hexdigest(),
        }
        self.manifests = self.root / 'manifests'
        self.manifests.mkdir()
        self.pin_path = self.manifests / '26.06-hgnc.json'
        self.pin_path.write_text(json.dumps(self.pin))
        self.destination = self.root / 'input' / hgnc.HGNC_FILENAME

    def test_fresh_download_is_verified_and_installed(self):
        with patch.object(hgnc.urllib.request, 'urlopen', return_value=io.BytesIO(self.data)) as get:
            hgnc.acquire(self.destination, self.pin)
        self.assertEqual(get.call_args.args[0].full_url, self.pin['url'])
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertFalse(self.destination.with_suffix('.txt.part').exists())

    def test_bad_download_is_not_installed_or_repinned(self):
        before = self.pin_path.read_bytes()
        with patch.object(hgnc.urllib.request, 'urlopen', return_value=io.BytesIO(b'changed')):
            with self.assertRaisesRegex(IngestError, 'checksum mismatch'):
                hgnc.acquire(self.destination, self.pin)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix('.txt.part').exists())
        self.assertEqual(self.pin_path.read_bytes(), before)

    def test_existing_copy_is_verified_without_network(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.data)
        with patch.object(hgnc.urllib.request, 'urlopen') as get:
            hgnc.acquire(self.destination, self.pin)
            get.assert_not_called()
            self.destination.write_bytes(self.data.replace(b'TEST', b'DRFT'))
            with self.assertRaisesRegex(IngestError, 'checksum mismatch'):
                hgnc.acquire(self.destination, self.pin)
            get.assert_not_called()

    def test_missing_copy_and_unknown_release_fail(self):
        with self.assertRaisesRegex(IngestError, 'opentargets.download_sources'):
            hgnc.verify(self.destination, self.pin)
        with self.assertRaisesRegex(IngestError, 'No HGNC pin'):
            hgnc.read_pin('99.99', self.manifests)

    def test_verify_checks_custom_hgnc_copy_offline(self):
        custom = self.root / 'elsewhere.txt'
        custom.write_bytes(self.data)
        argv = ['download_sources', '--verify', '--hgnc', str(custom),
                '--manifest-dir', str(self.manifests)]
        with patch('sys.argv', argv), patch.object(download_sources, 'verify_local', return_value=0), \
                patch.object(hgnc.urllib.request, 'urlopen') as get:
            self.assertEqual(download_sources.main(), 0)
            custom.write_bytes(b'changed')
            with self.assertRaisesRegex(IngestError, 'checksum mismatch'):
                download_sources.main()
            get.assert_not_called()

    def test_transform_uses_hgnc_in_custom_input_directory(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.data)
        argv = ['transform_mechanisms', '--indir', str(self.destination.parent),
                '--manifest-dir', str(self.manifests), '--workdir', str(self.root / 'output')]
        with patch('sys.argv', argv), patch.object(transform_mechanisms, 'transform') as transform:
            self.assertEqual(transform_mechanisms.main(), 0)
            self.assertEqual(transform.call_args.args[2].resolve('ENSG1'), ['HGNC:1'])
            self.destination.write_bytes(b'changed')
            transform.reset_mock()
            with self.assertRaisesRegex(IngestError, 'checksum mismatch'):
                transform_mechanisms.main()
            transform.assert_not_called()

    def test_release_metadata_records_hgnc_pin(self):
        path = self.root / 'release.json'
        download_sources.write_release_json(path, '26.06', 'anchor', '2026-06-23',
                                            'success', {}, self.pin)
        self.assertEqual(json.loads(path.read_text())['hgnc'], self.pin)


class PipelineHgncTests(unittest.TestCase):
    def test_custom_paths_reach_download_transform_and_metadata_loader(self):
        for skip in (False, True):
            with self.subTest(skip_download=skip):
                argv = ['pipeline', '--input-dir', '/tmp/ot-input', '--hgnc', '/tmp/pinned.txt',
                        '--manifest-dir', '/tmp/ot-manifests'] + (['--skip-download'] if skip else [])
                with patch('sys.argv', argv), patch.object(pipeline, 'run') as run:
                    self.assertEqual(pipeline.main(), 0)
                calls = {call.args[0]: call.args[1] for call in run.call_args_list}
                for module in ('download_sources', 'transform_mechanisms'):
                    args = calls[module]
                    self.assertEqual(args[args.index('--hgnc') + 1], '/tmp/pinned.txt')
                    self.assertEqual(args[args.index('--manifest-dir') + 1], '/tmp/ot-manifests')
                self.assertEqual('--verify' in calls['download_sources'], skip)
                load = calls['load_graph']
                self.assertEqual(load[load.index('--release-json') + 1], '/tmp/ot-input/release.json')


if __name__ == '__main__':
    unittest.main()
