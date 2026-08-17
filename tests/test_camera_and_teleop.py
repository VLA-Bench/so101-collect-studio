import time
import unittest
from pathlib import Path
from unittest.mock import patch

from collect_studio import cams as cams_module
from collect_studio import recorder as recorder_module
from collect_studio.arms import JOINTS


CAMERA_CONFIG = {
    "cameras": {"wrist": "cam-1", "left_rear": "cam-2", "right_rear": "cam-3"},
    "record": {"fps": 30, "width": 640, "height": 480},
}
DEVICES = [
    {"index": i, "unique_id": f"cam-{i + 1}", "name": f"Camera {i + 1}", "builtin": False}
    for i in range(3)
]


class FakeStream:
    def __init__(self, unique_id, width, height, fps, auto_exposure=False):
        self.unique_id = unique_id
        self.auto_exposure = auto_exposure
        self.ok = True
        self.err = None
        self.frame_count = 1
        self.stopped = False

    def stop(self):
        self.stopped = True

    def set_auto_exposure(self, enabled):
        self.auto_exposure = bool(enabled)
        return {"ok": True, "mode": "continuous" if enabled else "locked", "err": None}

    def exposure_status(self):
        return {
            "auto_exposure": self.auto_exposure,
            "ok": True,
            "mode": "continuous" if self.auto_exposure else "locked",
            "err": None,
        }


class CameraLifecycleTest(unittest.TestCase):
    def test_status_uses_cached_enumeration_and_stalled_stream_is_rebuilt(self):
        enumerations = []

        def enumerate_devices():
            enumerations.append(True)
            return [dict(d) for d in DEVICES]

        with (
            patch.object(cams_module, "enumerate_cameras", side_effect=enumerate_devices),
            patch.object(cams_module, "CamStream", FakeStream),
            patch.object(cams_module.config_store, "load", return_value=CAMERA_CONFIG),
        ):
            manager = cams_module.CamManager()
            manager.start_all()
            count_after_start = len(enumerations)
            manager.status()
            self.assertEqual(len(enumerations), count_after_start)

            old = manager.streams["cam-3"]
            old.ok = False
            old.err = "超过 3s 未收到帧"
            manager.retry_failed_bound()

            self.assertTrue(old.stopped)
            self.assertIsNot(manager.streams["cam-3"], old)
            self.assertEqual(manager.bound_health()["ready"], 3)


class FakeBus:
    def sync_read(self, _, **_kwargs):
        return {joint: 0.0 for joint in JOINTS}


class FakeFollower:
    def __init__(self):
        self.bus = FakeBus()
        self.sent = 0
        self.last_action = None

    def send_action(self, action):
        self.sent += 1
        self.last_action = dict(action)
        return dict(action)


class FakeLeader:
    def __init__(self):
        self.reads = 0

    def get_action(self):
        self.reads += 1
        return {f"{joint}.pos": 0.0 for joint in JOINTS}


class FakeArms:
    def __init__(self):
        self.leader = FakeLeader()
        self.follower = FakeFollower()
        self.torque_on = False
        self.current_reads = 0

    def read_leader_action(self):
        return self.leader.get_action()

    def read_follower_position(self):
        return self.follower.bus.sync_read("Present_Position")

    def send_action(self, action):
        return self.follower.send_action(action)

    def enable_torque(self):
        self.torque_on = True

    def estop(self):
        self.torque_on = False

    def read_gripper_current_ma(self):
        self.current_reads += 1
        return 0.0


class FailingArms(FakeArms):
    def __init__(self):
        super().__init__()
        self.estop_calls = 0

    def read_leader_action(self):
        raise ConnectionError("主动臂连续通信失败")

    def estop(self):
        super().estop()
        self.estop_calls += 1


class ReadyCams:
    def start_bound(self):
        pass

    def bound_health(self):
        return {"ready": 3, "total": 3, "problems": {}}

    def retry_failed_bound(self):
        pass


class FailedCams(ReadyCams):
    def __init__(self):
        self.retries = 0

    def bound_health(self):
        return {
            "ready": 2,
            "total": 3,
            "problems": {"right_rear": "超过 3s 未收到帧"},
        }

    def retry_failed_bound(self):
        self.retries += 1


class TeleopStartupTest(unittest.TestCase):
    @staticmethod
    def wait_for_start(service, timeout=1):
        deadline = time.monotonic() + timeout
        while service.teleop_starting and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_ready_only_after_control_loop_has_run(self):
        arms = FakeArms()
        service = recorder_module.RecordService(arms, ReadyCams())
        service.start_teleop()
        self.wait_for_start(service)

        self.assertTrue(service.teleop_on)
        self.assertGreater(arms.leader.reads, 0)
        self.assertGreater(arms.follower.sent, 0)
        service.stop_teleop()

    def test_camera_failure_names_the_role_and_reason(self):
        arms = FakeArms()
        cameras = FailedCams()
        service = recorder_module.RecordService(arms, cameras)
        with (
            patch.object(recorder_module, "CAMERA_READY_TIMEOUT", 0.3),
            patch.object(recorder_module, "CAMERA_RETRY_AFTER", 0.01),
            self.assertLogs("recorder", level="ERROR"),
        ):
            service.start_teleop()
            self.wait_for_start(service)

        self.assertFalse(service.teleop_on)
        self.assertIn("right_rear(超过 3s 未收到帧)", service.last_error)
        self.assertEqual(cameras.retries, 1)

    def test_persistent_arm_failure_estops(self):
        arms = FailingArms()
        service = recorder_module.RecordService(arms, ReadyCams())
        with self.assertLogs("recorder", level="ERROR"):
            service.start_teleop()
            self.wait_for_start(service)

        self.assertFalse(service.teleop_on)
        self.assertIn("主动臂连续通信失败", service.last_error)
        self.assertGreaterEqual(arms.estop_calls, 1)


class PreviewTransportTest(unittest.TestCase):
    def test_frontend_no_long_lived_mjpeg_streams(self):
        html = (Path(__file__).parents[1] / "static" / "index.html").read_text()
        self.assertNotIn(".mjpg", html)
        self.assertIn("AbortController", html)
        self.assertIn("/frame/role/", html)


if __name__ == "__main__":
    unittest.main()
