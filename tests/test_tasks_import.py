"""tasks.jsonl 导入:LeRobot 格式识别、重复跳过、坏行报错且不落盘。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collect_studio import library


class TasksImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jsn = Path(self.tmp.name) / "tasks.json"
        self.jsn.write_text(json.dumps([{"slug": "grab_the_cube", "prompt": "Grab the cube"}]))
        mock.patch.object(library, "TASKS_JSON", self.jsn).start()
        mock.patch.object(library, "DATA_ROOT", Path(self.tmp.name)).start()
        self.addCleanup(mock.patch.stopall)

    def _import(self, lines):
        return library.import_tasks("\n".join(lines))

    def test_lerobot_tasks_jsonl_format(self):
        r = self._import([
            json.dumps({"task_index": 0, "task": "Pick up the cube"}),
            json.dumps({"task_index": 1, "task": "Put it in the box"}),
        ])
        self.assertEqual((r["added"], r["skipped"]), (2, 0))
        slugs = [t["slug"] for t in library.load_tasks()]
        self.assertEqual(slugs, ["grab_the_cube", "pick_up_the_cube", "put_it_in_the_box"])

    def test_native_prompt_format_and_duplicates_skipped(self):
        r = self._import([
            json.dumps({"prompt": "Grab the cube"}),  # 与已有任务 slug 相同
            json.dumps({"task": "New task"}),
            json.dumps({"task": "New task"}),  # 文件内重复
        ])
        self.assertEqual((r["added"], r["skipped"]), (1, 2))

    def test_bad_line_raises_and_writes_nothing(self):
        before = self.jsn.read_text()
        with self.assertRaises(ValueError):
            self._import(['{"task": "ok"}', "not json", '{"task": "never reached"}'])
        self.assertEqual(self.jsn.read_text(), before)

    def test_line_missing_task_field_raises(self):
        with self.assertRaises(ValueError):
            self._import([json.dumps({"task_index": 3})])


if __name__ == "__main__":
    unittest.main()
