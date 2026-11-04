import contextlib
import hashlib
import io
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


class ModelIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = Path('tests/fixtures/bge-m3-config.json').read_bytes()
        self.model = self.root / 'config.json'
        self.model.write_bytes(self.content)
        source = Path('deploy/k8s/model.py').read_text()
        source = source.replace("pathlib.Path('/model/bge-m3')", f'pathlib.Path({str(self.root)!r})')
        expected = hashlib.sha256(self.content).hexdigest()
        self.source = re.sub(r'^files = .*$', f"files = {{'config.json': '{expected}'}}", source, flags=re.MULTILINE)

    def verify(self):
        with patch.object(sys, 'argv', ['model.py']), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(self.source, 'model.py', 'exec'), {})

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


if __name__ == '__main__':
    unittest.main()
