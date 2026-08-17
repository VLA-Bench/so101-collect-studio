"""井字棋 300 条实例:冻结清单、十八任务映射、同步、进度与 episode 身份。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from collect_studio import exporter, scene_view, tic_tac_toe
from collect_studio.recorder import RecordService


TASKS = [
    {
        "slug": tic_tac_toe.task_slug(side, cell),
        "prompt": f"Place the {side} cube in the {name} cell.",
        "set": "tic_tac_toe",
    }
    for side in ("black", "white")
    for cell, name in enumerate(tic_tac_toe.CELL_PROMPT_NAMES)
]


class FrozenScheduleTest(unittest.TestCase):
    def test_manifest_has_300_unique_instances_and_eighteen_task_mapping(self):
        self.assertEqual(
            hashlib.sha256(tic_tac_toe.TIC_TAC_TOE_MANIFEST.read_bytes()).hexdigest(),
            tic_tac_toe.SOURCE_MANIFEST_SHA256,
        )
        rows = tic_tac_toe.load_instances()
        self.assertEqual(len(rows), 300)
        self.assertEqual(len({row["state_id"] for row in rows}), 300)
        self.assertEqual(sum(row["side_to_move"] == "black" for row in rows), 156)
        self.assertEqual(sum(row["side_to_move"] == "white" for row in rows), 144)
        self.assertEqual(len({row["task_slug"] for row in rows}), 18)
        for row in rows:
            self.assertIsNone(row["board"][row["optimal_cell"]])
            self.assertEqual(row["task_slug"], tic_tac_toe.task_slug(row["side_to_move"], row["optimal_cell"]))
            self.assertEqual(row["layout_seed"], 100_000 + row["selection_index"])

    def test_progress_uses_selection_index_not_task_slug(self):
        row = tic_tac_toe.load_instances()[0]
        meta = tic_tac_toe.episode_metadata(row)
        episodes = [
            {"status": "saved", "task_set": "tic_tac_toe", "task_slug": row["task_slug"], "tic_tac_toe": meta},
            {"status": "saved", "task_set": "tic_tac_toe", "task_slug": row["task_slug"], "tic_tac_toe": meta},
            {"status": "trash", "task_set": "tic_tac_toe", "task_slug": row["task_slug"], "tic_tac_toe": meta},
        ]
        progress = tic_tac_toe.progress(episodes)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["duplicate_indices"], [0])
        self.assertEqual(progress["by_side"][row["side_to_move"]]["completed"], 1)

    def test_progress_ignores_instance_saved_under_wrong_task(self):
        row = tic_tac_toe.load_instances()[0]
        wrong_slug = tic_tac_toe.task_slug(row["side_to_move"], (row["optimal_cell"] + 1) % 9)
        progress = tic_tac_toe.progress([{
            "status": "saved", "task_set": "tic_tac_toe", "task_slug": wrong_slug,
            "tic_tac_toe": tic_tac_toe.episode_metadata(row),
        }])
        self.assertEqual(progress["completed"], 0)

    def test_progress_accepts_legacy_color_only_task_slug(self):
        row = tic_tac_toe.load_instances()[0]
        progress = tic_tac_toe.progress([{
            "status": "saved", "task_set": "tic_tac_toe",
            "task_slug": tic_tac_toe.LEGACY_TASK_SLUGS[row["side_to_move"]],
            "tic_tac_toe": tic_tac_toe.episode_metadata(row),
        }])
        self.assertEqual(progress["completed"], 1)

    def test_next_incomplete_wraps_in_manifest_order(self):
        self.assertEqual(tic_tac_toe.next_incomplete(0, [0, 1]), 2)
        self.assertEqual(tic_tac_toe.next_incomplete(299, [299]), 0)
        self.assertIsNone(tic_tac_toe.next_incomplete(10, list(range(300))))

    def test_task_slug_uses_physical_cell_names(self):
        names = (
            "far left", "far center", "far right",
            "middle left", "center", "middle right",
            "near left", "near center", "near right",
        )
        for side in ("black", "white"):
            for cell, name in enumerate(names):
                self.assertEqual(
                    tic_tac_toe.task_slug(side, cell),
                    f"{side}_cube_{name.replace(' ', '_')}",
                )


class TicTacToeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from collect_studio import server

        cls.server = server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(scene_view, "CURRENT_DISPLAY_JSON", Path(self.tmp.name) / "current_display.json").start()
        mock.patch.object(self.server.library, "load_tasks", lambda set_name=None: TASKS).start()
        mock.patch.object(self.server.library, "list_episodes", lambda: []).start()
        mock.patch.object(self.server.rec, "state", "idle").start()
        mock.patch.object(self.server.rec, "tic_tac_toe_pending", lambda index: False).start()
        mock.patch.object(self.server.rec, "tic_tac_toe_job_state", lambda index: None).start()
        self.addCleanup(mock.patch.stopall)

    def test_set_current_maps_instance_to_exact_cell_task(self):
        data = self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        current = data["current"]
        self.assertEqual(current["selection_index"], 0)
        self.assertEqual(current["task_slug"], "black_cube_far_center")
        self.assertEqual(current["instruction"], "Place the black cube in the far center cell.")
        self.assertEqual(scene_view.read_current()["selection_index"], 0)
        self.assertIsNone(current["board"][current["optimal_cell"]])

    def test_record_start_attaches_instance_metadata(self):
        self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        captured = []
        with mock.patch.object(self.server.rec, "rec_start", side_effect=lambda task: captured.append(task)), \
             mock.patch.object(self.server.rec, "status", return_value={"state": "rec"}):
            out = self.server.rec_start(self.server.RecStartReq(
                task_set="tic_tac_toe", task_slug="black_cube_far_center",
            ))
        self.assertEqual(out["state"], "rec")
        self.assertEqual(captured[0]["prompt"], "Place the black cube in the far center cell.")
        self.assertEqual(captured[0]["tic_tac_toe"]["selection_index"], 0)
        self.assertEqual(captured[0]["tic_tac_toe"]["state_id"], tic_tac_toe.instance(0)["state_id"])

    def test_record_start_rejects_wrong_side_task(self):
        self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        with self.assertRaises(HTTPException) as caught:
            self.server.rec_start(self.server.RecStartReq(
                task_set="tic_tac_toe", task_slug="white_cube_center",
            ))
        self.assertEqual(caught.exception.status_code, 400)

    def test_record_start_rejects_completed_instance(self):
        row = tic_tac_toe.instance(0)
        self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        episode = {
            "status": "saved", "task_set": "tic_tac_toe", "task_slug": "black_cube_far_center",
            "tic_tac_toe": tic_tac_toe.episode_metadata(row),
        }
        with mock.patch.object(self.server.library, "list_episodes", return_value=[episode]):
            with self.assertRaises(HTTPException) as caught:
                self.server.rec_start(self.server.RecStartReq(
                    task_set="tic_tac_toe", task_slug="black_cube_far_center",
                ))
        self.assertIn("已有有效", caught.exception.detail)

    def test_record_start_rejects_instance_while_encoding(self):
        self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        with mock.patch.object(self.server.rec, "tic_tac_toe_pending", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                self.server.rec_start(self.server.RecStartReq(
                task_set="tic_tac_toe", task_slug="black_cube_far_center",
                ))
        self.assertIn("正在后台编码", caught.exception.detail)

    def test_switch_rejected_while_recording(self):
        self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=0))
        with mock.patch.object(self.server.rec, "state", "paused"):
            with self.assertRaises(HTTPException) as caught:
                self.server.tic_tac_toe_set_current(self.server.TicTacToeCurrentReq(selection_index=1))
        self.assertIn("不能切换", caught.exception.detail)


class RecorderAndExporterMetadataTest(unittest.TestCase):
    def test_recorder_meta_keeps_tic_tac_toe_identity(self):
        service = object.__new__(RecordService)
        service.ep_id = "episode_000001"
        service.session = "session"
        service.cur_task = {
            "slug": "black_cube_far_center", "set": "tic_tac_toe",
            "prompt": "Place the black cube in the far center cell.",
            "tic_tac_toe": tic_tac_toe.episode_metadata(tic_tac_toe.instance(0)),
        }
        service.rows = [object()]
        service.rec_elapsed = 1.0
        with mock.patch("collect_studio.recorder.config_store.load", return_value={
            "record": {"fps": 30, "width": 640, "height": 480},
        }):
            meta = service._meta_dict()
        self.assertEqual(meta["tic_tac_toe"]["selection_index"], 0)
        self.assertEqual(meta["task_slug"], "black_cube_far_center")
        self.assertEqual(meta["task_prompt"], "Place the black cube in the far center cell.")

    def test_encoding_state_is_tracked_by_instance(self):
        service = object.__new__(RecordService)
        service.encode_q = [
            {"state": "done", "tic_tac_toe": {"selection_index": 0}},
            {"state": "encoding", "tic_tac_toe": {"selection_index": 1}},
        ]
        self.assertFalse(service.tic_tac_toe_pending(0))
        self.assertTrue(service.tic_tac_toe_pending(1))

    def test_export_sidecar_keeps_instance_mapping(self):
        meta = tic_tac_toe.episode_metadata(tic_tac_toe.instance(0))
        row = exporter._tic_tac_toe_export_entry(7, {"id": "episode_000001", "tic_tac_toe": meta})
        self.assertEqual(row["episode_index"], 7)
        self.assertEqual(row["source_episode_id"], "episode_000001")
        self.assertEqual(row["selection_index"], 0)
        self.assertIsNone(exporter._tic_tac_toe_export_entry(0, {"id": "episode_000002"}))


if __name__ == "__main__":
    unittest.main()
