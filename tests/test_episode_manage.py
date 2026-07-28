"""episode 管理:改提示词 / 归入已有任务(改 set/slug 并移目录)、彻底删除(仅回收站)。

目录为四层结构 <root>/<task_set>/<task_slug>/<session>/<ep_id>。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collect_studio import library


def _mk_ep(root, slug, session, ep_id, prompt="p", task_set="默认"):
    d = root / task_set / slug / session / ep_id
    d.mkdir(parents=True)
    meta = {"id": ep_id, "session": session, "task_slug": slug, "task_set": task_set,
            "task_prompt": prompt, "frames": 3, "dur": 1.0, "created_at": "2026-07-20 10:00"}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


class EpisodeManageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = self.root / "library"
        self.trash = self.root / "trash"
        self.lib.mkdir()
        self.trash.mkdir()
        # 任务集合:old_task / new_task 均存在(slug 由 prompt 派生),供归入校验
        (self.root / "tasks.json").write_text(json.dumps([{"prompt": "Old task"}, {"prompt": "New task"}]))
        # 第二个集合里有同名 slug 的任务,验证 (set, slug) 定位
        (self.root / "tasks_kitchen.json").write_text(json.dumps([{"prompt": "Old task"}]))
        mock.patch.object(library, "DATA_ROOT", self.root).start()
        mock.patch.object(library, "TASKS_JSON", self.root / "tasks.json").start()
        mock.patch.object(library, "LIBRARY", self.lib).start()
        mock.patch.object(library, "TRASH", self.trash).start()
        self.addCleanup(mock.patch.stopall)

    def test_update_prompt_only_keeps_slug_and_dir(self):
        d = _mk_ep(self.lib, "old_task", "s1", "episode_000000", prompt="Old task")
        m = library.update_episode("episode_000000", task_prompt="Fixed prompt")
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["task_prompt"], "Fixed prompt")
        self.assertEqual(meta["task_slug"], "old_task")
        self.assertEqual(m["dir"], str(d))

    def test_reassign_moves_dir_and_uses_target_prompt(self):
        _mk_ep(self.lib, "old_task", "s1", "episode_000001", prompt="Old task")
        m = library.update_episode("episode_000001", task_slug="new_task")
        self.assertFalse((self.lib / "默认" / "old_task" / "s1" / "episode_000001").exists())
        dst = self.lib / "默认" / "new_task" / "s1" / "episode_000001"
        meta = json.loads((dst / "meta.json").read_text())
        self.assertEqual(meta["task_slug"], "new_task")
        self.assertEqual(meta["task_set"], "默认")
        self.assertEqual(meta["task_prompt"], "New task")  # 未给 prompt 时取目标任务的 prompt
        self.assertEqual(m["dir"], str(dst))

    def test_reassign_to_other_set_moves_into_set_dir(self):
        """同名 slug 存在于多个集合时,task_set 指定目标集合,目录移到该集合层下。"""
        _mk_ep(self.lib, "old_task", "s1", "episode_000010", prompt="Old task")
        m = library.update_episode("episode_000010", task_slug="old_task", task_set="kitchen")
        dst = self.lib / "kitchen" / "old_task" / "s1" / "episode_000010"
        self.assertEqual(m["dir"], str(dst))
        meta = json.loads((dst / "meta.json").read_text())
        self.assertEqual(meta["task_set"], "kitchen")
        self.assertEqual(meta["task_slug"], "old_task")

    def test_reassign_with_explicit_prompt(self):
        _mk_ep(self.lib, "old_task", "s1", "episode_000002", prompt="Old task")
        library.update_episode("episode_000002", task_prompt="Custom", task_slug="new_task")
        meta = json.loads((self.lib / "默认" / "new_task" / "s1" / "episode_000002" / "meta.json").read_text())
        self.assertEqual(meta["task_prompt"], "Custom")

    def test_reassign_rejects_unknown_slug(self):
        _mk_ep(self.lib, "old_task", "s1", "episode_000003")
        with self.assertRaises(ValueError):
            library.update_episode("episode_000003", task_slug="ghost")

    def test_reassign_inside_trash_stays_in_trash(self):
        _mk_ep(self.trash, "old_task", "s1", "episode_000004", prompt="Old task")
        m = library.update_episode("episode_000004", task_slug="new_task")
        self.assertTrue((self.trash / "new_task" / "s1" / "episode_000004").exists())
        self.assertEqual(m["status"], "trash")

    def test_update_rejects_empty_prompt(self):
        _mk_ep(self.lib, "old_task", "s1", "episode_000005")
        with self.assertRaises(ValueError):
            library.update_episode("episode_000005", task_prompt="  ")

    def test_update_missing_episode(self):
        with self.assertRaises(FileNotFoundError):
            library.update_episode("episode_999999", task_prompt="x")

    def test_delete_only_from_trash(self):
        _mk_ep(self.lib, "old_task", "s1", "episode_000006")
        with self.assertRaises(ValueError):  # 已保存的不能彻底删,需先标废
            library.delete_episode("episode_000006")
        d = _mk_ep(self.trash, "old_task", "s1", "episode_000007")
        library.delete_episode("episode_000007")
        self.assertFalse(d.exists())


if __name__ == "__main__":
    unittest.main()
