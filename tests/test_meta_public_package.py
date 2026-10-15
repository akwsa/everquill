import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MetaPublicPackageTests(unittest.TestCase):
    def test_public_file_selection_is_explicit(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        selected = project['tool']['hatch']['build'].get('only-include')
        self.assertIsInstance(selected, list)
        self.assertTrue(selected)
        self.assertTrue(all('*' not in path for path in selected))
        self.assertNotIn('docs', selected)
        self.assertNotIn('skills', selected)
        self.assertNotIn('README.md', selected)
        for path in selected:
            self.assertNotIn('.bak', path)
            self.assertNotIn('muse_backend.py', path)
            self.assertNotIn('gateway.py', path)
            self.assertNotIn('routes.json', path)
            self.assertNotIn('desc0.bin', path)
            self.assertNotIn('desc1.bin', path)
            self.assertFalse(path.startswith(('src/muse_cli/', 'src/muse_code/')))

    def test_public_manifest_matches_config_and_links_close(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        manifest = json.loads((ROOT / 'docs/public/package-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(project['tool']['hatch']['build']['only-include'], manifest['allowed_files'])
        self.assertEqual(set(project['tool']['hatch']['build']['targets']['wheel']['only-include']),
                         set(manifest['runtime_files']))
        self.assertEqual(len(manifest['allowed_files']), len(set(manifest['allowed_files'])))
        self.assertTrue(set(manifest['runtime_files']).issubset(manifest['allowed_files']))
        for value in manifest['runtime_files']:
            self.assertTrue(value.startswith(('src/everquill/', 'src/everquill_gateway/')))
        import re
        for name in ('README.md', 'README.id.md', 'BOUNDARY.md'):
            document = ROOT / 'docs/public' / name
            for target in re.findall(r'\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
                if '://' not in target:
                    resolved = (document.parent / target).resolve().relative_to(ROOT).as_posix()
                    self.assertIn(resolved, manifest['allowed_files'])
                    self.assertTrue((ROOT / resolved).is_file())

    def test_public_metadata_has_no_legacy_runtime_dependencies(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertEqual(project['project']['dependencies'], [])
        self.assertEqual(project['project']['readme'], 'docs/public/README.md')
        self.assertIn('Meta', project['project']['description'])
        self.assertNotIn('Protocol Notes', project['project']['urls'])


if __name__ == '__main__':
    unittest.main()
