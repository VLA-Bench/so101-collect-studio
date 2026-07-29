from __future__ import annotations

import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from collect_studio import recorder as recorder_module
from collect_studio.arms import ArmManager, JOINTS
from collect_studio.gripper_protection import (
    DEFAULT_GRIPPER_PROTECTION,
    GripperProtection,
    validate_gripper_config,
)


class GripperProtectionTest(unittest.TestCase):
    def config(self, **changes):
        config = dict(DEFAULT_GRIPPER_PROTECTION)
        config.update(changes)
        return config

    def test_disabled_passes_requested_target(self):
        protection = GripperProtection(self.config(enabled=False))
        applied, state, event = protection.apply(0, 20, 800, 0.0)
        self.assertEqual(applied, 0)
        self.assertFalse(state["protection_active"])
        self.assertIsNone(event)

    def test_spike_movement_and_opening_do_not_trigger(self):
        protection = GripperProtection(self.config())
        protection.apply(0, 20, 800, 0.0)
        protection.apply(0, 20, 0, 0.1)
        _, state, _ = protection.apply(0, 20, 800, 0.4)
        self.assertFalse(state["protection_active"])

        protection.reset()
        for index in range(5):
            _, state, _ = protection.apply(0, 20 - index, 800, index * 0.1)
        self.assertFalse(state["protection_active"])

        protection.reset()
        for index in range(5):
            _, state, _ = protection.apply(30, 20, 800, index * 0.1)
        self.assertFalse(state["protection_active"])

    def test_stall_holds_until_leader_opens_past_margin(self):
        protection = GripperProtection(self.config())
        for now, position in ((0.0, 20.0), (0.1, 20.2), (0.2, 20.1), (0.31, 20.2)):
            applied, state, event = protection.apply(0, position, 800, now)
        self.assertEqual(applied, 20.2)
        self.assertTrue(state["protection_active"])
        self.assertEqual(event, "activated")

        applied, state, event = protection.apply(0, 20.2, 0, 0.5)
        self.assertEqual(applied, 20.2)
        self.assertTrue(state["protection_active"])
        self.assertIsNone(event)

        applied, state, event = protection.apply(22.1, 20.2, 0, 0.6)
        self.assertEqual(applied, 20.2)
        self.assertTrue(state["protection_active"])
        self.assertIsNone(event)

        applied, state, event = protection.apply(22.2, 20.2, 0, 0.7)
        self.assertEqual(applied, 22.2)
        self.assertFalse(state["protection_active"])
        self.assertEqual(event, "released")

    def test_reconfigure_preserves_hold_but_disable_and_reset_release(self):
        protection = GripperProtection(self.config())
        protection.apply(0, 20, 800, 0.0)
        protection.apply(0, 20, 800, 0.31)
        protection.configure(self.config(current_limit_ma=400))
        applied, state, _ = protection.apply(0, 20, 0, 0.4)
        self.assertEqual(applied, 20)
        self.assertTrue(state["protection_active"])

        protection.configure(self.config(enabled=False))
        self.assertFalse(protection.active)
        protection.configure(self.config())
        protection.apply(0, 20, 800, 1.0)
        protection.apply(0, 20, 800, 1.31)
        protection.reset()
        self.assertFalse(protection.active)

    def test_validation_rejects_invalid_values(self):
        for field, value in (
            ("current_limit_ma", 0),
            ("stall_window_ms", 0),
            ("max_position_change", -1),
            ("min_closing_error", 101),
            ("release_margin", float("nan")),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_gripper_config(self.config(**{field: value}))


class ArmCurrentReadTest(unittest.TestCase):
    def test_raw_current_is_converted_to_milliamps(self):
        bus = Mock()
        bus.sync_read.return_value = {"gripper": -12}
        manager = ArmManager()
        manager.follower = Mock(bus=bus)
        manager.lock = threading.RLock()
        self.assertEqual(manager.read_gripper_current_ma(), 78.0)
        bus.sync_read.assert_called_once_with(
            "Present_Current", motors=["gripper"], normalize=False
        )


class FakeBus:
    def __init__(self):
        self.position = {joint: float(index + 1) for index, joint in enumerate(JOINTS)}
        self.position["gripper"] = 20.0

    def sync_read(self, data_name, **_kwargs):
        if data_name != "Present_Position":
            raise AssertionError(data_name)
        return dict(self.position)


class FakeFollower:
    def __init__(self):
        self.bus = FakeBus()
        self.sent = []

    def send_action(self, action):
        self.sent.append(dict(action))
        return dict(action)


class StoppingLeader:
    def __init__(self, action, stop_after):
        self.action = action
        self.stop_after = stop_after
        self.reads = 0
        self.stop_event = None

    def get_action(self):
        self.reads += 1
        if self.reads >= self.stop_after:
            self.stop_event.set()
        return dict(self.action)


class FakeArms:
    def __init__(self, action, stop_after=1):
        self.leader = StoppingLeader(action, stop_after)
        self.follower = FakeFollower()
        self.current_reads = 0
        self.torque_on = False

    def read_gripper_current_ma(self):
        self.current_reads += 1
        return 800.0

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


class FakeCams:
    pass


class RecordServiceGripperTest(unittest.TestCase):
    def config(self):
        return {
            "record": {"fps": 100, "width": 640, "height": 480},
            "gripper_protection": dict(DEFAULT_GRIPPER_PROTECTION),
        }

    def test_loop_only_clamps_sent_gripper_and_records_leader_request(self):
        action = {f"{joint}.pos": float(index + 10) for index, joint in enumerate(JOINTS)}
        action["gripper.pos"] = 0.0
        arms = FakeArms(action)
        with patch.object(recorder_module.config_store, "load", return_value=self.config()):
            service = recorder_module.RecordService(arms, FakeCams())
            arms.leader.stop_event = service._stop
            service.gripper_protection.active = True
            service.gripper_protection.hold_position = 20.0
            service.state = "rec"
            service._capture_frame = Mock()
            service._stop.clear()
            service._loop()

        sent = arms.follower.sent[0]
        for joint in JOINTS[:-1]:
            self.assertEqual(sent[f"{joint}.pos"], action[f"{joint}.pos"])
        self.assertEqual(sent["gripper.pos"], 20.0)
        recorded_action = service._capture_frame.call_args.args[1]
        self.assertEqual(recorded_action["gripper.pos"], 0.0)
        self.assertEqual(service.gripper["requested_target"], 0.0)
        self.assertEqual(service.gripper["applied_target"], 20.0)

    def test_current_is_sampled_every_three_frames(self):
        action = {f"{joint}.pos": 50.0 for joint in JOINTS}
        arms = FakeArms(action, stop_after=7)
        config = self.config()
        config["gripper_protection"]["enabled"] = False
        with patch.object(recorder_module.config_store, "load", return_value=config):
            service = recorder_module.RecordService(arms, FakeCams())
            arms.leader.stop_event = service._stop
            service._stop.clear()
            service._loop()
        self.assertEqual(arms.leader.reads, 7)
        self.assertEqual(arms.current_reads, 3)

    def test_gripper_protection_activation_is_logged(self):
        action = {f"{joint}.pos": 0.0 for joint in JOINTS}
        arms = FakeArms(action)
        with patch.object(recorder_module.config_store, "load", return_value=self.config()):
            service = recorder_module.RecordService(arms, FakeCams())
            arms.leader.stop_event = service._stop
            service.gripper_protection.apply = Mock(
                return_value=(0.0, service.gripper_protection.snapshot(), "activated")
            )
            service._stop.clear()
            with self.assertLogs("recorder", level="WARNING") as logs:
                service._loop()

        self.assertTrue(any("触发夹爪保护" in line for line in logs.output))

    def test_settings_are_validated_persisted_and_applied(self):
        action = {f"{joint}.pos": 0.0 for joint in JOINTS}
        arms = FakeArms(action)
        with patch.object(recorder_module.config_store, "load", return_value=self.config()):
            service = recorder_module.RecordService(arms, FakeCams())
        changed = copy.deepcopy(DEFAULT_GRIPPER_PROTECTION)
        changed["current_limit_ma"] = 450
        with patch.object(recorder_module.config_store, "update") as update:
            saved = service.update_gripper_config(changed)
        update.assert_called_once()
        self.assertEqual(saved["current_limit_ma"], 450)
        self.assertEqual(service.status()["gripper"]["config"]["current_limit_ma"], 450)


class FrontendGripperTest(unittest.TestCase):
    def test_frontend_has_two_line_telemetry_and_settings(self):
        html = (
            Path(__file__).parents[1] / "static" / "index.html"
        ).read_text()
        self.assertEqual(html.count('id="gripper-line1"'), 1)
        self.assertEqual(html.count('id="gripper-line2"'), 1)
        self.assertIn('id="gp-enabled"', html)
        self.assertIn("/api/config/gripper-protection", html)


if __name__ == "__main__":
    unittest.main()
