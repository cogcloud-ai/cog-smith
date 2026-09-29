"""Generated packages retain machinery licensing across caller license choices."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import smith_core
import smith_op


class LicensingTests(unittest.TestCase):
    def test_cog_license_override_preserves_smith_license(self):
        for kind in ('context-cog', 'code-cog', 'decision-cog'):
            if not (ROOT / 'templates' / kind / 'src').is_dir():
                continue  # Older published Smith versions only support context Cogs.
            for license_id in ('Apache-2.0', 'MIT'):
                with self.subTest(kind=kind, license=license_id), tempfile.TemporaryDirectory() as tmp:
                    tokens = smith_core.default_tokens('cog-license-test')
                    self.assertEqual(tokens['LICENSE'], 'Apache-2.0')
                    tokens['LICENSE'] = license_id
                    dest = Path(tmp) / 'cog-license-test'
                    smith_core.create(dest, tokens, template=kind)
                    self.assertIn('license = "' + license_id + '"', (dest / 'pixi.toml').read_text())
                    self.assertEqual((dest / 'LICENSE.smith').read_bytes(), (ROOT / 'LICENSE').read_bytes())
                    self.assertIn('Copyright 2026 OpenTeams', (dest / 'NOTICE.smith').read_text())

    def test_op_plan_includes_machinery_license(self):
        from test_smith_op import SmithOpCase
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / 'spec.yaml'
            spec.write_text(yaml.safe_dump(SmithOpCase().spec_doc()))
            _, files = smith_op.plan(spec, Path(tmp) / 'op-license-test')
            self.assertEqual(files['LICENSE.smith'], (ROOT / 'LICENSE').read_text())
            self.assertIn('NOTICE.smith', files)
            self.assertIn('LICENSING.md', files)
