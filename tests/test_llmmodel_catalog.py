"""LLMModel CRs -> catalog -> minted descriptors (hub note, step 1).

The generator is pack-neutral (stdlib + pyyaml, no smith imports); the
integration test proves its output feeds mint_from_config and the minted
descriptors pass the checker's descriptor rules.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import llmmodel_catalog  # noqa: E402
import smith_check       # noqa: E402
import smith_models      # noqa: E402

LLMMODEL = """\
apiVersion: llm.nebari.dev/v1alpha1
kind: LLMModel
metadata:
  name: devstral-small
  namespace: nebari-llm-serving-system
spec:
  model:
    name: "mistralai/Devstral-Small-2505"
    source: huggingface
  resources:
    gpu: {count: 1, type: nvidia}
  serving: {replicas: 1}
  access:
    public: false
    groups: ["developers"]
"""

KUBECTL_LIST = """\
apiVersion: v1
kind: List
items:
  - apiVersion: llm.nebari.dev/v1alpha1
    kind: LLMModel
    metadata: {name: qwen35b, namespace: ns}
    spec:
      model: {name: "Qwen/Qwen3.5-35B-A3B-GPTQ-Int4"}
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: not-a-model}
"""


class TestGenerator(unittest.TestCase):
    def test_single_doc_internal_surface(self):
        models = list(llmmodel_catalog._iter_docs(LLMMODEL, "t"))
        cat = llmmodel_catalog.build_catalog(
            models, "internal", "cluster.example.com", "NEBARI_LLM_JWT")
        (entry,) = cat["models"]
        self.assertEqual(entry["cog_name"], "cog-devstral-small")
        self.assertEqual(entry["model_name"], "mistralai/Devstral-Small-2505")
        self.assertEqual(entry["served_model_id"], entry["model_name"])
        self.assertEqual(entry["endpoint"],
                         "https://llm-internal.cluster.example.com/v1")
        self.assertEqual(entry["runtime"], "vllm")
        self.assertEqual(entry["locality"], "customer-vpc")
        self.assertIsNone(entry["revision"])

    def test_kubectl_list_skips_other_kinds(self):
        docs = [d for d in llmmodel_catalog._iter_docs(KUBECTL_LIST, "t")
                if d.get("kind") == "LLMModel"]
        self.assertEqual(len(docs), 1)
        cat = llmmodel_catalog.build_catalog(
            docs, "install-time", None, "MODEL_API_KEY")
        self.assertEqual(cat["models"][0]["address"], "install-time")
        self.assertNotIn("endpoint", cat["models"][0])

    def test_internal_surface_requires_base_domain(self):
        models = list(llmmodel_catalog._iter_docs(LLMMODEL, "t"))
        with self.assertRaises(llmmodel_catalog.LLMModelError):
            llmmodel_catalog.build_catalog(models, "internal", None, "X_Y")

    def test_dotted_k8s_name_is_sanitized(self):
        doc = {"kind": "LLMModel",
               "metadata": {"name": "qwen3.5-mini", "namespace": "ns"},
               "spec": {"model": {"name": "q/mini"}}}
        cat = llmmodel_catalog.build_catalog(
            [doc], "install-time", None, "MODEL_API_KEY")
        self.assertEqual(cat["models"][0]["cog_name"], "cog-qwen3-5-mini")

    def test_duplicate_names_refused(self):
        doc = {"kind": "LLMModel", "metadata": {"name": "m", "namespace": "n"},
               "spec": {"model": {"name": "x/y"}}}
        with self.assertRaises(llmmodel_catalog.LLMModelError):
            llmmodel_catalog.build_catalog(
                [doc, dict(doc)], "install-time", None, "MODEL_API_KEY")


class TestEndToEnd(unittest.TestCase):
    def test_generated_catalog_mints_passing_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "llmmodels"
            src.mkdir()
            (src / "devstral.yaml").write_text(LLMMODEL)
            (src / "list.yaml").write_text(KUBECTL_LIST)
            out = Path(tmp) / "catalog.yaml"
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "llmmodel_catalog.py"),
                 str(src), "--surface", "internal",
                 "--base-domain", "cluster.example.com",
                 "--owner", "trent@openteams.com", "-o", str(out)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            result = smith_models.mint_from_config(out, Path(tmp) / "models")
            self.assertEqual(sorted(result["minted"]),
                             ["cog-devstral-small", "cog-qwen35b"])
            for name in result["minted"]:
                findings = smith_check.check(Path(tmp) / "models" / name)
                errors = [f for f in findings if f["level"] == "error"]
                self.assertEqual(errors, [], (name, errors))

    def test_stdout_output_is_valid_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "m.yaml"
            f.write_text(LLMMODEL)
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "llmmodel_catalog.py"),
                 str(f)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            import yaml
            cat = yaml.safe_load(r.stdout)
            self.assertEqual(len(cat["models"]), 1)


if __name__ == "__main__":
    unittest.main()
