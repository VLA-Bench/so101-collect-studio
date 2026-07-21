"""任务集合:tasks_<名称>.json 枚举、合并列表带 set 字段(不跨集合去重)、指定集合读写、集合名防路径穿越。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collect_studio import library


class TaskSetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jsn = self.root / "tasks.json"
        self.jsn.write_text(json.dumps([{"slug": "grab_the_cube", "prompt": "Grab the cube"}]))
        (self.root / "tasks_kitchen.json").write_text(json.dumps([
            {"slug": "wash_the_plate", "prompt": "Wash the plate"},
            {"slug": "grab_the_cube", "prompt": "重复 slug"},
        ]))
        mock.patch.object(library, "DATA_ROOT", self.root).start()
        mock.patch.object(library, "TASKS_JSON", self.jsn).start()
        self.addCleanup(mock.patch.stopall)

    def test_list_task_sets(self):
        self.assertEqual(library.list_task_sets(), ["默认", "kitchen"])

    def test_load_all_keeps_cross_set_duplicates_with_set_field(self):
        """合并列表不跨集合去重:相同 slug 在不同集合是不同任务,各保留一条。"""
        tasks = library.load_tasks()
        self.assertEqual(
            [(t["slug"], t["set"]) for t in tasks],
            [("grab_the_cube", "默认"), ("wash_the_plate", "kitchen"), ("grab_the_cube", "kitchen")],
        )

    def test_load_single_set(self):
        tasks = library.load_tasks("kitchen")
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["set"] == "kitchen" for t in tasks))

    def test_add_task_writes_into_named_set(self):
        library.add_task("Wipe the table", set_name="kitchen")
        slugs = [t["slug"] for t in json.loads((self.root / "tasks_kitchen.json").read_text())]
        self.assertIn("wipe_the_table", slugs)

    def test_add_task_allows_slug_from_other_set(self):
        """跨集合允许同 slug(数据按 (set, slug) 隔离),返回的任务带 set 字段。"""
        t = library.add_task("wash the plate")  # slug 已存在于 kitchen,但写入「默认」允许
        self.assertEqual(t["slug"], "wash_the_plate")
        self.assertEqual(t["set"], "默认")
        slugs = [x["slug"] for x in json.loads(self.jsn.read_text())]
        self.assertIn("wash_the_plate", slugs)

    def test_add_task_rejects_slug_within_same_set(self):
        with self.assertRaises(ValueError):
            library.add_task("wash the plate", set_name="kitchen")  # 同集合内重复仍报错

    def test_import_into_named_set_allows_cross_set_duplicates(self):
        """跨集合重复不再跳过;同集合内重复仍跳过。"""
        r = library.import_tasks(
            '{"task": "Grab the cube"}\n{"task": "Pour water"}', set_name="lab")
        self.assertEqual((r["added"], r["skipped"]), (2, 0))  # grab_the_cube 与其他集合重复但不跳过
        slugs = [t["slug"] for t in json.loads((self.root / "tasks_lab.json").read_text())]
        self.assertEqual(slugs, ["grab_the_cube", "pour_water"])
        r = library.import_tasks('{"task": "Pour water"}', set_name="lab")
        self.assertEqual((r["added"], r["skipped"]), (0, 1))  # 同集合重复跳过

    def test_grouped_keeps_cross_set_duplicates(self):
        """分组视图不去重:kitchen 里与默认集重复的任务仍保留(前端按集合过滤依赖它)。"""
        grouped = library.load_tasks_grouped()
        self.assertEqual([t["slug"] for t in grouped["默认"]], ["grab_the_cube"])
        self.assertEqual([t["slug"] for t in grouped["kitchen"]],
                         ["wash_the_plate", "grab_the_cube"])
        # 合并视图同样保留跨集合重复(任务身份 = (set, slug))
        self.assertEqual([t["set"] for t in library.load_tasks()], ["默认", "kitchen", "kitchen"])

    def test_illegal_set_name_rejected(self):
        with self.assertRaises(ValueError):
            library.load_tasks("../etc")

    def test_jsonl_default_set_enumerated_and_parsed(self):
        self.jsn.unlink()
        (self.root / "tasks.jsonl").write_text(
            '{"task_index": 0, "task": "Pick the cube"}\n{"task_index": 1, "task": "Place it"}\n')
        self.assertEqual(library.list_task_sets(), ["默认", "kitchen"])
        tasks = library.load_tasks("默认")
        self.assertEqual([t["slug"] for t in tasks], ["pick_the_cube", "place_it"])

    def test_named_jsonl_set_enumerated(self):
        (self.root / "tasks_lab.jsonl").write_text('{"task_index": 0, "task": "Sort the vial"}\n')
        self.assertEqual(library.list_task_sets(), ["默认", "kitchen", "lab"])
        self.assertEqual(library.load_tasks("lab")[0]["slug"], "sort_the_vial")

    def test_json_array_without_slug_derives_slug(self):
        self.jsn.write_text(json.dumps([{"prompt": "Grab the cube"}]))
        self.assertEqual(library.load_tasks("默认")[0]["slug"], "grab_the_cube")

    def test_explicit_slug_preserved(self):
        self.jsn.write_text(json.dumps([{"slug": "custom_id", "prompt": "Grab the cube"}]))
        self.assertEqual(library.load_tasks("默认")[0]["slug"], "custom_id")

    def test_json_preferred_over_jsonl_on_conflict(self):
        (self.root / "tasks_kitchen.jsonl").write_text('{"task_index": 0, "task": "Should be ignored"}\n')
        prompts = [t["prompt"] for t in library.load_tasks("kitchen")]
        self.assertNotIn("Should be ignored", prompts)

    def test_add_task_to_jsonl_set_writes_lerobot_jsonl(self):
        self.jsn.unlink()
        jl = self.root / "tasks.jsonl"
        jl.write_text('{"task_index": 0, "task": "Pick the cube"}\n')
        library.add_task("Place it")
        lines = [json.loads(l) for l in jl.read_text().splitlines() if l.strip()]
        self.assertEqual([(l["task_index"], l["task"]) for l in lines],
                         [(0, "Pick the cube"), (1, "Place it")])


if __name__ == "__main__":
    unittest.main()
