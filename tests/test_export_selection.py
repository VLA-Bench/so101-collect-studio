"""导出选择:按任务合并汇总(trash 不计)、task-only 选择匹配该任务全部批次。"""
import unittest
from unittest import mock

from collect_studio import exporter


def _ep(task, session, ep_id, dur=10.0, status="saved"):
    return {"task_slug": task, "session": session, "id": ep_id, "dur": dur, "status": status}


EPS = [
    _ep("task_a", "s1", "episode_000000", 30.0),
    _ep("task_a", "s2", "episode_000001", 30.0),
    _ep("task_b", "s2", "episode_000002", 60.0),
    _ep("task_b", "s2", "episode_000003", 60.0, status="trash"),
]


class ExportSelectionTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(exporter.library, "list_episodes", lambda: EPS).start()
        self.addCleanup(mock.patch.stopall)

    def test_tasks_summary_merges_sessions_and_skips_trash(self):
        out = {t["task_slug"]: t for t in exporter.tasks_summary()}
        self.assertEqual(set(out), {"task_a", "task_b"})
        self.assertEqual(out["task_a"]["count"], 2)
        self.assertEqual(out["task_a"]["sessions"], 2)
        self.assertEqual(out["task_a"]["minutes"], 1.0)
        self.assertEqual(out["task_b"]["count"], 1)  # trash 不计入

    def test_task_only_selection_matches_all_sessions(self):
        eps = exporter._selected_episodes([{"task_slug": "task_a"}])
        self.assertEqual([e["id"] for e in eps], ["episode_000000", "episode_000001"])

    def test_session_selection_still_supported(self):
        eps = exporter._selected_episodes([{"task_slug": "task_a", "session": "s2"}])
        self.assertEqual([e["id"] for e in eps], ["episode_000001"])

    def test_episode_id_selection(self):
        eps = exporter._selected_episodes([{"episode": "episode_000002"}])
        self.assertEqual([e["id"] for e in eps], ["episode_000002"])

    def test_mixed_episode_and_task_selection(self):
        eps = exporter._selected_episodes([{"episode": "episode_000002"}, {"task_slug": "task_a"}])
        self.assertEqual([e["id"] for e in eps], ["episode_000000", "episode_000001", "episode_000002"])

    def test_empty_selection_means_all_saved(self):
        self.assertEqual(len(exporter._selected_episodes([])), 3)


class UnitsMetadataTest(unittest.TestCase):
    """info.json 的 units 字段:逐 feature 物理量说明,与仿真数据集对齐。"""

    def test_units_covers_all_features(self):
        units = exporter.build_units_json("base")
        self.assertEqual(set(units), {
            "observation.pos_state", "pos_action",
            "observation.eef_state", "eef_action",
            "timestamp", "frame_index", "episode_index", "index", "task_index",
            *(f"observation.images.{r}" for r in exporter.ROLES),
        })

    def test_joint_units_are_normalized_not_radian(self):
        units = exporter.build_units_json("base")
        self.assertEqual(units["observation.pos_state"]["unit"], "normalized_m100_100")
        self.assertEqual(units["pos_action"]["quantity"], "absolute_joint_position_target")
        self.assertEqual(units["pos_action"]["component_order"], exporter.JOINT_ORDER)

    def test_eef_units_match_fk_convention(self):
        units = exporter.build_units_json("base")
        eef = units["observation.eef_state"]
        self.assertEqual(eef["coordinate_frame"], "robot_base")
        self.assertEqual(eef["rotation_representation"], "euler_rpy")
        self.assertEqual(eef["rotation_continuity"], "wrapped_to_pi_no_unwrap")
        self.assertEqual(eef["component_layout"][6]["unit"], "normalized_0_100")


if __name__ == "__main__":
    unittest.main()
