import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_model_script():
    spec = importlib.util.spec_from_file_location(
        'embedding_model_integrity', Path('deploy/k8s/model.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.module = load_model_script()
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = Path('tests/fixtures/bge-m3-config.json').read_bytes()
        self.model = self.root / 'config.json'
        self.model.write_bytes(self.content)
        self.files = {'config.json': hashlib.sha256(self.content).hexdigest()}

    def verify(self):
        self.module.verify(self.root, self.files)

    def test_valid_file_passes(self):
        self.verify()

    def test_corrupted_file_stops_startup(self):
        self.model.write_bytes(b'corrupt')
        with self.assertRaisesRegex(SystemExit, 'SHA256 mismatch'):
            self.verify()

    def test_missing_file_stops_startup(self):
        self.model.unlink()
        with self.assertRaises(FileNotFoundError):
            self.verify()

    def test_unreadable_file_stops_startup(self):
        original = Path.open
        def deny_model(path, *args, **kwargs):
            if path == self.model:
                raise PermissionError('unreadable model')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', deny_model), self.assertRaises(PermissionError):
            self.verify()

    def test_unexpected_file_stops_startup(self):
        (self.root / 'extra').write_bytes(b'extra')
        with self.assertRaisesRegex(SystemExit, 'Unexpected model files'):
            self.verify()

    def test_model_specs_are_pinned_and_share_one_verifier(self):
        assert set(self.module.MODELS) == {'bge-m3', 'bge-small-zh-v1.5'}
        bge_m3 = self.module.MODELS['bge-m3']
        small = self.module.MODELS['bge-small-zh-v1.5']
        assert bge_m3['root'] == Path('/model/bge-m3')
        assert bge_m3['revision'] == '5617a9f61b028005a4858fdac845db406aefb181'
        assert small['root'] == Path('/model/bge-small-zh-v1.5')
        assert small['repo'] == 'BAAI/bge-small-zh-v1.5'
        assert small['revision'] == '7999e1d3359715c523056ef9478215996d62a620'
        assert len(small['revision']) == 40
        assert small['files']['pytorch_model.bin'] == (
            '7c5fe667bbed05dc10e246e229b701ad266fe4d95ab946e9e5aa402056611b88'
        )
        for model in (bge_m3, small):
            assert len(model['revision']) == 40
            assert all(
                len(checksum) == 64
                and set(checksum) <= set('0123456789abcdef')
                for checksum in model['files'].values()
            )

    def test_small_model_config_is_pinned_to_512_dimensions(self):
        content = Path('tests/fixtures/bge-small-zh-v1.5-config.json').read_bytes()
        spec = self.module.MODELS['bge-small-zh-v1.5']
        assert hashlib.sha256(content).hexdigest() == spec['files']['config.json']
        assert json.loads(content)['hidden_size'] == 512


if __name__ == '__main__':
    unittest.main()
