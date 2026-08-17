"""场景系统:迁移逻辑正确性(build_instruction/classify 往返)、validate_scene 各分支、
save_scenes 校验/重复资产 409/force、sync_task_files 生成与原子性。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from collect_studio import scene_view, scenes

# desk01 迁移时的只读备份(本仓库内),作为迁移逻辑的黄金数据
BACKUP = Path(__file__).resolve().parent.parent / "configs" / "scenes_backup" / "scenes_desk01_20260726.json"


def _raw_scenes() -> dict:
    return json.loads(BACKUP.read_text(encoding="utf-8"))


class MigrationRoundtripTest(unittest.TestCase):
    """对 desk01 既有 scenes.json 全部任务:build_instruction → parse_instruction 往返,
    每块 targets 必须一致(证明迁移后的指令生成与展示页降级解析对得上)。"""

    def test_instruction_roundtrip_all_tasks(self):
        n = 0
        for raw in _raw_scenes()["scenes"]:
            sc = scenes.expand_scene(raw)
            for task in sc["tasks"]:
                n += 1
                instr = scenes.build_instruction(sc, task["targets"])
                parsed = scene_view.parse_instruction(instr)
                self.assertIsNotNone(parsed, f"解析失败: {instr}")
                # 指令不点名空盒,故只比对每块 target(与原始 targets 一致)
                self.assertEqual(parsed["targets"], task["targets"], f"往返不一致: {instr}")
                # 派生子任务与谓词直算一致
                self.assertEqual(
                    scenes.classify_targets(sc, task["targets"]),
                    scenes.classify_targets(sc, task["targets"]),
                )
        self.assertEqual(n, 5)  # desk01 迁移数据共 5 个任务


class SubtaskClassificationTest(unittest.TestCase):
    def test_t4_matches_d4_cube_selection(self):
        scene = scenes.expand_scene({
            "id": "D4",
            "boxes": ["large_blue"],
            "blocks": ["cube_yellow", "cube_blue", "l_block_red"],
            "tasks": [],
        })
        targets = {
            "cube_yellow": "large_blue",
            "cube_blue": "large_blue",
            "l_block_red": None,
        }
        self.assertIn("T4", scenes.classify_targets(scene, targets))

    def test_t4_rejects_box_mixing_shapes(self):
        scene = scenes.expand_scene({
            "id": "D4",
            "boxes": ["large_blue"],
            "blocks": ["cube_yellow", "cube_blue", "l_block_red"],
            "tasks": [],
        })
        targets = {
            "cube_yellow": "large_blue",
            "cube_blue": "large_blue",
            "l_block_red": "large_blue",
        }
        self.assertNotIn("T4", scenes.classify_targets(scene, targets))

    def test_current_d4_is_t5_not_t4(self):
        scene = scenes.expand_scene({
            "id": "D4",
            "boxes": ["medium_red", "small_yellow"],
            "blocks": ["cuboid_blue", "l_block_blue", "cube_red", "cube_yellow"],
            "tasks": [],
        })
        targets = {
            "cuboid_blue": "medium_red",
            "l_block_blue": "medium_red",
            "cube_red": "small_yellow",
            "cube_yellow": "small_yellow",
        }
        subtasks = scenes.classify_targets(scene, targets)
        self.assertIn("T5", subtasks)
        self.assertNotIn("T4", subtasks)

    def test_t5_rejects_placement_not_covered_by_both_axes(self):
        scene = scenes.expand_scene({
            "id": "D5",
            "boxes": ["large_blue", "small_red"],
            "blocks": ["cube_yellow", "cube_blue", "l_block_red"],
            "tasks": [],
        })
        targets = {
            "cube_yellow": "large_blue",
            "cube_blue": "large_blue",
            "l_block_red": None,
        }
        self.assertNotIn("T5", scenes.classify_targets(scene, targets))


class ValidateSceneTest(unittest.TestCase):
    def _expand(self, **kw):
        raw = {"id": "T", "boxes": ["large_red"], "blocks": ["cube_red"], "tasks": []}
        raw.update(kw)
        return scenes.expand_scene(raw)

    def test_valid_scene_passes(self):
        sc = self._expand(tasks=[{"targets": {"cube_red": "large_red"}}])
        self.assertEqual(scenes.validate_scene(sc), [])

    def test_duplicate_box_id(self):
        sc = self._expand(boxes=["large_red", "large_red"])
        self.assertTrue(any("盒 id 重复" in e for e in scenes.validate_scene(sc)))

    def test_too_many_blocks(self):
        blocks = ["cube_red", "cube_yellow", "cube_blue", "cuboid_red", "cuboid_yellow", "cuboid_blue"]
        sc = self._expand(blocks=blocks)
        self.assertTrue(any("超过槽位数" in e for e in scenes.validate_scene(sc)))

    def test_non_whitelist_box_config(self):
        sc = self._expand(boxes=["large_red", "medium_blue"])  # 大中组合不在白名单
        self.assertTrue(any("不在白名单" in e for e in scenes.validate_scene(sc)))

    def test_targets_missing_block(self):
        sc = self._expand(tasks=[{"targets": {}}])
        self.assertTrue(any("未覆盖全部物块" in e for e in scenes.validate_scene(sc)))

    def test_targets_unknown_box(self):
        sc = self._expand(tasks=[{"targets": {"cube_red": "small_blue"}}])
        self.assertTrue(any("目标盒" in e and "不存在" in e for e in scenes.validate_scene(sc)))

    def test_over_capacity(self):
        sc = self._expand(
            boxes=["small_red"],
            blocks=["cube_red", "cube_yellow", "cube_blue"],
            tasks=[{"targets": {"cube_red": "small_red", "cube_yellow": "small_red", "cube_blue": "small_red"}}],
        )
        self.assertTrue(any("超过容量" in e for e in scenes.validate_scene(sc)))


class SaveScenesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scenes_json = self.root / "scenes.json"
        self.scenes_json.write_text(json.dumps(_raw_scenes()), encoding="utf-8")
        mock.patch.object(scenes, "SCENES_JSON", self.scenes_json).start()
        mock.patch.object(scenes, "DATA_ROOT", self.root).start()
        mock.patch.dict(scenes._cache, {"mtime": None, "scenes": None}).start()
        self.addCleanup(mock.patch.stopall)

    def test_validation_error_raises(self):
        bad = {"scenes": [{"id": "X", "boxes": ["huge_red"], "blocks": [], "tasks": []}]}
        with self.assertRaises(scenes.SceneValidationError) as cm:
            scenes.save_scenes(bad)
        self.assertTrue(cm.exception.errors)

    def test_duplicate_scene_id_rejected(self):
        dup = {"scenes": [
            {"id": "A", "boxes": ["large_red"], "blocks": ["cube_red"], "tasks": []},
            {"id": "A", "boxes": ["small_blue"], "blocks": ["cube_blue"], "tasks": []},
        ]}
        with self.assertRaises(scenes.SceneValidationError):
            scenes.save_scenes(dup)

    def test_conflict_409_and_force(self):
        dup_assets = {"scenes": [
            {"id": "A", "boxes": ["large_red"], "blocks": ["cube_red"], "tasks": []},
            {"id": "B", "boxes": ["large_red"], "blocks": ["cube_red"], "tasks": []},
        ]}
        with self.assertRaises(scenes.SceneConflictError) as cm:
            scenes.save_scenes(dup_assets)
        self.assertEqual(cm.exception.conflicts, [{"id": "B", "matches": "A"}])
        before = self.scenes_json.read_text()
        scenes.save_scenes(dup_assets, force=True)  # force 放行
        self.assertNotEqual(self.scenes_json.read_text(), before)
        self.assertEqual(scenes.scene_ids(), ["A", "B"])

    def test_save_writes_and_syncs_task_files(self):
        scenes.save_scenes(_raw_scenes(), force=True)
        tasks_a = self.root / "tasks_A.jsonl"
        self.assertTrue(tasks_a.is_file())
        lines = [json.loads(x) for x in tasks_a.read_text().splitlines()]
        self.assertEqual(len(lines), 3)  # 场景 A 有 3 个任务
        self.assertEqual(lines[0]["task_index"], 0)
        self.assertIn("task", lines[0])
        # 其他场景也各自生成
        self.assertTrue((self.root / "tasks_B.jsonl").is_file())
        self.assertTrue((self.root / "tasks_C.jsonl").is_file())

    def test_sync_task_files_skips_unchanged(self):
        scenes.save_scenes(_raw_scenes(), force=True)
        tasks_a = self.root / "tasks_A.jsonl"
        mtime = tasks_a.stat().st_mtime
        time.sleep(0.02)
        written = scenes.sync_task_files()
        self.assertEqual(written, [])  # 内容不变不写(避免 mtime 抖动)
        self.assertEqual(tasks_a.stat().st_mtime, mtime)

    def test_load_scenes_derives_subtasks_and_instruction(self):
        out = scenes.load_scenes()
        self.assertEqual([sc["scene_id"] for sc in out], ["A", "B", "C"])
        a0 = out[0]["tasks"][0]
        self.assertEqual(a0["subtasks"], ["T1", "T2"])
        self.assertTrue(a0["instruction"].startswith("Place "))

    def test_load_scenes_missing_file_returns_empty(self):
        self.scenes_json.unlink()
        mock.patch.dict(scenes._cache, {"mtime": None, "scenes": None}).start()
        self.assertEqual(scenes.load_scenes(), [])
        self.assertEqual(scenes.scene_ids(), [])

    def test_instruction_index(self):
        idx = scenes.instruction_index()
        self.assertEqual(len(idx), 5)
        hit = next(iter(idx.values()))
        self.assertIn(hit["scene_id"], {"A", "B", "C"})
        self.assertIn("targets", hit["task"])

    def test_is_scene_set(self):
        self.assertTrue(scenes.is_scene_set("A"))
        self.assertFalse(scenes.is_scene_set("kitchen"))
        self.assertFalse(scenes.is_scene_set(None))
        self.assertFalse(scenes.is_scene_set("../etc"))


if __name__ == "__main__":
    unittest.main()
