"""场景展示页:parse_instruction 文本解析、scenes.json 精确匹配(完整场景含空盒)、
以及 POST /api/display/current → GET /api/display/scene 链路。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collect_studio import scene_view, scenes


class ParseInstructionTest(unittest.TestCase):
    def test_multi_blocks_multi_boxes(self):
        out = scene_view.parse_instruction(
            "Place red cube and red rectangular block into the large red box, "
            "and blue cube into the small blue box"
        )
        self.assertEqual([b["id"] for b in out["boxes"]], ["large_red", "small_blue"])
        self.assertEqual(
            [(b["id"], b["target"]) for b in out["blocks"]],
            [("cube_red", "large_red"), ("cuboid_red", "large_red"), ("cube_blue", "small_blue")],
        )
        self.assertEqual(out["targets"]["cuboid_red"], "large_red")

    def test_left_on_desk_blocks(self):
        out = scene_view.parse_instruction(
            "Place red cube into the medium yellow box, and leave blue cube and yellow L-shaped block on the desk"
        )
        self.assertEqual([b["id"] for b in out["boxes"]], ["medium_yellow"])
        self.assertEqual(out["targets"], {
            "cube_red": "medium_yellow",
            "cube_blue": None,
            "l_block_yellow": None,
        })

    def test_leave_only_instruction(self):
        out = scene_view.parse_instruction("Leave red cube and blue rectangular block on the desk")
        self.assertEqual(out["boxes"], [])
        self.assertEqual(
            [(b["id"], b["target"]) for b in out["blocks"]],
            [("cube_red", None), ("cuboid_blue", None)],
        )

    def test_oxford_comma_clauses(self):
        # 三个放置子句:", " 与 ", and " 混合连接;子句内部多物块用 " and "
        out = scene_view.parse_instruction(
            "Place red cube into the large red box, blue cube into the small blue box, "
            "and yellow cube into the medium yellow box"
        )
        self.assertEqual([b["id"] for b in out["boxes"]], ["large_red", "small_blue", "medium_yellow"])
        self.assertEqual(len(out["blocks"]), 3)

    def test_unparseable_prompt_returns_none(self):
        self.assertIsNone(scene_view.parse_instruction("把红色方块放进大盒子"))
        self.assertIsNone(scene_view.parse_instruction(""))
        self.assertIsNone(scene_view.parse_instruction(None))
        self.assertIsNone(scene_view.parse_instruction("Place the thing somewhere"))

    def test_payload_structure(self):
        out = scene_view.payload("Place red cube into the large red box")
        self.assertEqual(out["instruction"], "Place red cube into the large red box")
        self.assertEqual(out["scene"]["boxes"][0]["id"], "large_red")
        # meta 为本地硬编码常量(数值来自 desk01 scene.py)
        self.assertEqual(out["meta"]["layout"]["BOX_CAPACITY"]["large"], 4)
        self.assertIn("red", out["meta"]["color_hex"])
        miss = scene_view.payload("无法解析的提示词")
        self.assertIsNone(miss["scene"])
        self.assertIsNotNone(miss["meta"])


class CurrentDisplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current_json = Path(self.tmp.name) / "current_display.json"
        mock.patch.object(scene_view, "CURRENT_DISPLAY_JSON", self.current_json).start()
        self.addCleanup(mock.patch.stopall)

    def test_write_read_current(self):
        data = scene_view.write_current("默认", "slug_a", "Place red cube into the large red box")
        self.assertEqual(data["prompt"], "Place red cube into the large red box")
        got = scene_view.read_current()
        self.assertEqual(got["task_slug"], "slug_a")
        self.assertEqual(got["task_set"], "默认")


class DisplayApiTest(unittest.TestCase):
    """POST /api/display/current → GET /api/display/scene 链路。

    环境未装 httpx(starlette TestClient 依赖),故直接调用路由处理函数——
    路由本身只是薄封装,链路语义一致。"""

    @classmethod
    def setUpClass(cls):
        from collect_studio import server

        cls.server = server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(scene_view, "CURRENT_DISPLAY_JSON", Path(self.tmp.name) / "current_display.json").start()
        # scenes.json 指向不存在的 tmp 路径,隔离真实 ~/so101_data/scenes.json
        mock.patch.object(scenes, "SCENES_JSON", Path(self.tmp.name) / "scenes.json").start()
        mock.patch.dict(scenes._cache, {"mtime": None, "scenes": None}).start()
        self.addCleanup(mock.patch.stopall)

    def test_post_then_get(self):
        prompt = "Place red cube and red rectangular block into the large red box, and leave the blue cube on the desk"
        out = self.server.display_current(self.server.DisplayCurrentReq(
            task_set="默认", task_slug="place_red", prompt=prompt,
        ))
        self.assertEqual(out["prompt"], prompt)
        data = self.server.display_scene()
        self.assertEqual(data["current"]["prompt"], prompt)
        self.assertEqual(data["scene"]["targets"], {
            "cube_red": "large_red", "cuboid_red": "large_red", "cube_blue": None,
        })
        self.assertIsNotNone(data["meta"])

    def test_get_without_current(self):
        data = self.server.display_scene()
        self.assertIsNone(data["current"])
        self.assertIsNone(data["scene"])

    def test_unmatched_prompt(self):
        self.server.display_current(self.server.DisplayCurrentReq(prompt="随便一个任务"))
        data = self.server.display_scene()
        self.assertIsNone(data["scene"])
        self.assertEqual(data["instruction"], "随便一个任务")


class DisplayScenesHitTest(unittest.TestCase):
    """命中 scenes.json:返回完整场景(含未被点名的干扰空盒与派生 subtasks)。"""

    SCENES = {"scenes": [{
        "id": "X",
        "boxes": ["large_red", "small_blue"],  # small_blue 空盒(无块指向),指令不点名
        "blocks": ["cube_red", "cube_blue"],
        "tasks": [{"targets": {"cube_red": "large_red", "cube_blue": None}}],
    }]}
    INSTRUCTION = "Place red cube into the large red box, and leave blue cube on the desk"

    @classmethod
    def setUpClass(cls):
        from collect_studio import server

        cls.server = server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "scenes.json").write_text(json.dumps(self.SCENES), encoding="utf-8")
        mock.patch.object(scenes, "SCENES_JSON", root / "scenes.json").start()
        mock.patch.object(scenes, "DATA_ROOT", root).start()
        mock.patch.dict(scenes._cache, {"mtime": None, "scenes": None}).start()
        mock.patch.object(scene_view, "CURRENT_DISPLAY_JSON", root / "current_display.json").start()
        self.addCleanup(mock.patch.stopall)

    def test_hit_returns_full_scene_with_empty_box(self):
        self.server.display_current(self.server.DisplayCurrentReq(prompt=self.INSTRUCTION))
        data = self.server.display_scene()
        sc = data["scene"]
        self.assertEqual(sc["scene_id"], "X")
        self.assertEqual(sc["subtasks"], ["T2"])  # 有留桌块 → T1 不成立;放置块与盒同色 → T2
        self.assertEqual([b["id"] for b in sc["boxes"]], ["large_red", "small_blue"])  # 空盒也在
        self.assertEqual(sc["targets"], {"cube_red": "large_red", "cube_blue": None})
        self.assertEqual([b["target"] for b in sc["blocks"]], ["large_red", None])

    def test_miss_falls_back_to_parse(self):
        self.server.display_current(self.server.DisplayCurrentReq(
            prompt="Place yellow cube into the medium yellow box"))
        data = self.server.display_scene()
        self.assertIsNone(data["scene"].get("scene_id"))  # 解析路径无 scene_id
        self.assertEqual(data["scene"]["boxes"][0]["id"], "medium_yellow")


if __name__ == "__main__":
    unittest.main()
