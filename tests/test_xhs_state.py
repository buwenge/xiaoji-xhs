import json
import tempfile
import unittest
from pathlib import Path

from xhs import state as state_store


class LoadSaveTests(unittest.TestCase):
    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            loaded = state_store.load(path)
            self.assertEqual(loaded, state_store.DEFAULT_STATE)

    def test_corrupt_json_returns_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = state_store.load(path)
            self.assertEqual(loaded, state_store.DEFAULT_STATE)

    def test_non_dict_json_returns_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            loaded = state_store.load(path)
            self.assertEqual(loaded, state_store.DEFAULT_STATE)

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            payload = {**state_store.DEFAULT_STATE, "tokens_today": 4200, "day": "2026-09-22"}
            state_store.save(payload, path)
            self.assertEqual(state_store.load(path), payload)

    def test_save_is_atomic_no_leftover_tmp_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            state_store.save({"day": "2026-09-22"}, path)
            leftovers = [p for p in Path(tempdir).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_partial_state_merges_with_defaults(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            path.write_text(json.dumps({"tokens_today": 999}), encoding="utf-8")
            loaded = state_store.load(path)
            self.assertEqual(loaded["tokens_today"], 999)
            self.assertEqual(loaded["current_note"], None)
            self.assertEqual(loaded["reported"], [])

    def test_default_path_uses_xhs_paths_state_file(self):
        # 不传 path= 的调用点应该回读 xhs.paths.state_file()（已被
        # conftest 改道到临时目录），这里只验证不抛异常、返回默认形状。
        loaded = state_store.load()
        self.assertIn("tokens_today", loaded)


if __name__ == "__main__":
    unittest.main()
