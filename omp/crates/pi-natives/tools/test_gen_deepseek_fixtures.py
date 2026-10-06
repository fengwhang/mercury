"""Exercise the fixture CLI with local HF-format tokenizers, without model assets."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tokenizers import Tokenizer, models, pre_tokenizers

SCRIPT = pathlib.Path(__file__).with_name("gen-deepseek-fixtures.py")


class LocalTokenizerGeneration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mercury-deepseek-local-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.tokenizer = self.root / "local.tokenizer.json"
        # Byte-complete BPE with reserved sentinel ids, not a downloaded model.
        vocab = {"<sentinel-0>": 0, "<sentinel-1>": 1, "<sentinel-2>": 2}
        vocab.update({char: index for index, char in enumerate(sorted(pre_tokenizers.ByteLevel.alphabet()), 3)})
        tokenizer = Tokenizer(models.BPE(vocab=vocab, merges=[]))
        tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        tokenizer.save(str(self.tokenizer))
        self.output = self.root / "output.json"

    def invoke(self, v4, v3):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--tokenizer-v4", str(v4), "--tokenizer-v3", str(v3), "--output", str(self.output)],
            capture_output=True, text=True, timeout=30,
        )

    def test_missing_v4_is_actionable_and_writes_nothing(self):
        result = self.invoke(self.root / "missing-v4.json", self.tokenizer)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DeepSeek V4 tokenizer not found", result.stderr)
        self.assertIn("--tokenizer-v4", result.stderr)
        self.assertIn("no assets are downloaded", result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_v3_does_not_download_or_write_output(self):
        missing = self.root / "missing-v3.json"
        result = self.invoke(self.tokenizer, missing)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DeepSeek V3 tokenizer not found", result.stderr)
        self.assertIn("--tokenizer-v3", result.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(self.output.exists())

    def test_provided_local_tokenizers_generate_deterministically(self):
        first = self.invoke(self.tokenizer, self.tokenizer)
        self.assertEqual(first.returncode, 0, first.stderr)
        data = self.output.read_bytes()
        fixture = json.loads(data)
        self.assertEqual(fixture["cases"][0], {"text": "", "ids": [], "count": 0})
        self.assertTrue(any(case["text"] == "1234" and case["count"] == 4 for case in fixture["cases"]))
        for case in fixture["cases"] + [fixture["v3_parity"]]:
            self.assertEqual(case["count"], len(case["ids"]))
            self.assertTrue(all(token_id >= 3 for token_id in case["ids"]))
        second = self.invoke(self.tokenizer, self.tokenizer)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(data, self.output.read_bytes())


if __name__ == "__main__":
    unittest.main()
