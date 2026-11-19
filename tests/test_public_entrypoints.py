"""Public entrypoints and the release boundary need no image engine or network."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/spektra.py'


def cli(*arguments):
    result = subprocess.run([sys.executable, str(CLI), *arguments], capture_output=True, encoding='utf-8')
    return result.returncode, json.loads(result.stdout)


class PublicEntrypoints(unittest.TestCase):
    def test_tutorial_works_without_runtime(self):
        code, result = cli('menu')
        self.assertEqual(code, 0)
        self.assertTrue(result['tutorial_markdown'])
        self.assertTrue(result['menu_markdown'])

    def test_film_choice_and_explicit_adjustment(self):
        code, result = cli('plan', '--effect', '2', '--grain', '0')
        self.assertEqual(code, 0)
        self.assertEqual(result['settings']['film'], 'kodak_gold_200')
        self.assertEqual(result['settings']['grain'], 0)

    def test_unsupported_choice_returns_error_not_substitution(self):
        code, result = cli('plan', '--effect', 'not-a-film')
        self.assertNotEqual(code, 0)
        self.assertEqual(result['status'], 'error')

    def test_archive_excludes_machine_and_user_files(self):
        spec = importlib.util.spec_from_file_location('zip_builder', ROOT / 'tools/build_skill_zip.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            root.mkdir()
            for name in builder.TOP_FILES:
                shutil.copyfile(ROOT / name, root / name)
            for name in builder.DIRECTORIES:
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'runtime.local.json').write_text('{"private":"machine-settings"}', encoding='utf-8')
            (root / 'samples').mkdir()
            (root / 'samples/private.jpg').write_bytes(b'user-photo')
            archive = Path(temporary) / 'skill.zip'
            builder.build(root, archive)
            with zipfile.ZipFile(archive) as zipped:
                self.assertIn('spektra-color-grade/SKILL.md', zipped.namelist())
                self.assertFalse(any('runtime.local.json' in name or 'private.jpg' in name for name in zipped.namelist()))


if __name__ == '__main__':
    unittest.main()
