"""Model-cog classification is declared, never inferred.

Descriptor rules key off the F5 marker (model.descriptor: true) — a
weight-carrying model cog (weights + serving task, no smith machinery)
must not be judged by descriptor rules (locality, install-time address,
api_key_env), and a kind: model manifest that declares neither weights
nor the marker gets a warning asking it to say which it is. Regression
for the cog-qwen3b misclassification found 2026-08-23.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_check  # noqa: E402

FRONTMATTER = """---
type: cog [0.1]
name: {name}
description: test model cog
version: "0.1.0"
manifest: cog.yaml
manifest_schema: openteams/cog-manifest [0.1]
---
# {name}
"""

BASE = {
    "schema": "openteams/cog-manifest [0.1]",
    "id": "openteams/cog-weights-toy",
    "version": "0.1.0",
    "kind": "model",
    "summary": "toy",
    "owner": "t@example.com",
    "license": "BSD-3-Clause",
    "interfaces": [{"name": "web-api", "kind": "openai-compatible",
                    "task": "serve",
                    "endpoint": "http://127.0.0.1:8080/v1",
                    "served_model_id": "toy", "default": True}],
    "provides": ["model-endpoint/openai-compatible"],
}


def write_cog(tmp, name, manifest):
    root = Path(tmp) / name
    root.mkdir()
    (root / "COG.md").write_text(FRONTMATTER.format(name=name))
    (root / "cog.yaml").write_text(yaml.safe_dump(manifest))
    return root


class TestModelClassification(unittest.TestCase):
    def test_weight_carrier_is_not_descriptor_checked(self):
        m = dict(BASE)
        m["model"] = {"name": "Toy-3B", "quantization": "Q4",
                      "runtime": "llama.cpp",
                      "weights": {"source": "https://example.com/toy.gguf",
                                  "sha256": "0" * 64}}
        with tempfile.TemporaryDirectory() as tmp:
            root = write_cog(tmp, "cog-weights-toy", m)
            findings = smith_check.check(root)
            self.assertFalse(
                [f for f in findings if f["check"] == "descriptor"],
                findings)
            self.assertFalse([f for f in findings if f["level"] == "error"],
                             findings)

    def test_marker_still_triggers_descriptor_rules(self):
        m = dict(BASE)
        m["model"] = {"name": "Toy-3B", "descriptor": True}
        # no locality, no served address rules satisfied -> descriptor errors
        with tempfile.TemporaryDirectory() as tmp:
            root = write_cog(tmp, "cog-weights-toy", m)
            findings = smith_check.check(root)
            self.assertTrue(
                [f for f in findings
                 if f["check"] == "descriptor" and f["level"] == "error"],
                findings)

    def test_neither_weights_nor_marker_warns(self):
        m = dict(BASE)
        m["model"] = {"name": "Toy-3B"}
        with tempfile.TemporaryDirectory() as tmp:
            root = write_cog(tmp, "cog-weights-toy", m)
            findings = smith_check.check(root)
            self.assertTrue(
                [f for f in findings
                 if f["level"] == "warn" and f["check"] == "descriptor"
                 and "declare which" in f["detail"]],
                findings)


class TestCodeKind(unittest.TestCase):
    """kind: code (decided 2026-09-17) is a model-free Cog: declared, never
    inferred, and refused when it declares a model requirement."""

    def _code_manifest(self, requires):
        return {
            "schema": "openteams/cog-manifest [0.1]",
            "id": "openteams/cog-code-toy", "version": "0.1.0",
            "kind": "code", "summary": "toy", "owner": "t@example.com",
            "license": "BSD-3-Clause", "requires": requires,
            "io": {"accepts": ["repo-config"], "produces": ["github-items"]},
            "interfaces": [{"name": "run", "kind": "cli", "task": "run",
                            "audience": "usage", "default": True}],
        }

    def test_code_cog_with_model_requirement_is_an_error(self):
        m = self._code_manifest([{"capability": "llm-chat"}])
        with tempfile.TemporaryDirectory() as tmp:
            root = write_cog(tmp, "cog-code-toy", m)
            findings = smith_check.check(root)
            self.assertTrue(
                [f for f in findings if f["level"] == "error"
                 and f["check"] == "declarations"
                 and "kind: code" in f["detail"]], findings)

    def test_code_cog_without_requires_is_not_asked_about_a_model(self):
        m = self._code_manifest([])
        with tempfile.TemporaryDirectory() as tmp:
            root = write_cog(tmp, "cog-code-toy", m)
            findings = smith_check.check(root)
            self.assertFalse(
                [f for f in findings if f["check"] == "declarations"
                 and "model dependency" in f["detail"]], findings)


if __name__ == "__main__":
    unittest.main()
