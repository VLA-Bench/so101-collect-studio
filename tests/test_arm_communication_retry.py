import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from collect_studio.arms import ArmManager, BUS_NUM_RETRY, JOINTS


class ArmCommunicationRetryTest(unittest.TestCase):
    def setUp(self):
        self.manager = ArmManager()
        self.manager.lock = threading.RLock()

    def test_teleop_reads_use_three_retries(self):
        leader_bus = Mock()
        leader_bus.sync_read.return_value = {
            joint: float(index) for index, joint in enumerate(JOINTS)
        }
        follower_bus = Mock()
        follower_bus.sync_read.return_value = {
            joint: float(index + 10) for index, joint in enumerate(JOINTS)
        }
        self.manager.leader = Mock(bus=leader_bus)
        self.manager.follower = Mock(bus=follower_bus)

        action = self.manager.read_leader_action()
        position = self.manager.read_follower_position()

        leader_bus.sync_read.assert_called_once_with("Present_Position")
        follower_bus.sync_read.assert_called_once_with("Present_Position")
        self.assertEqual(action["shoulder_pan.pos"], 0.0)
        self.assertEqual(position["shoulder_pan"], 10.0)

    def test_action_write_uses_three_retries(self):
        bus = Mock()
        follower = SimpleNamespace(
            bus=bus,
            config=SimpleNamespace(max_relative_target=None),
        )
        self.manager.follower = follower
        action = {
            f"{joint}.pos": float(index)
            for index, joint in enumerate(JOINTS)
        }

        sent = self.manager.send_action(action)

        bus.sync_write.assert_called_once_with(
            "Goal_Position",
            {joint: float(index) for index, joint in enumerate(JOINTS)},
        )
        self.assertEqual(sent, action)

    def test_each_retry_and_recovery_are_logged(self):
        bus = Mock()
        bus.sync_read.side_effect = [
            ConnectionError("丢包 1"),
            ConnectionError("丢包 2"),
            {joint: 0.0 for joint in JOINTS},
        ]
        self.manager.leader = Mock(bus=bus)

        with self.assertLogs("arms", level="INFO") as logs:
            self.manager.read_leader_action()

        self.assertEqual(bus.sync_read.call_count, 3)
        self.assertTrue(any("第 1/3 次重试" in line for line in logs.output))
        self.assertTrue(any("第 2/3 次重试" in line for line in logs.output))
        self.assertTrue(any("第 2/3 次重试后恢复" in line for line in logs.output))

    def test_retry_exhaustion_is_logged_and_raised(self):
        bus = Mock()
        bus.sync_read.side_effect = ConnectionError("线路断开")
        self.manager.follower = Mock(bus=bus)

        with (
            self.assertLogs("arms", level="WARNING") as logs,
            self.assertRaises(ConnectionError),
        ):
            self.manager.read_follower_position()

        self.assertEqual(bus.sync_read.call_count, BUS_NUM_RETRY + 1)
        for retry in range(1, BUS_NUM_RETRY + 1):
            self.assertTrue(
                any(f"第 {retry}/{BUS_NUM_RETRY} 次重试" in line for line in logs.output)
            )
        self.assertTrue(any("已用尽 3 次重试" in line for line in logs.output))

    def test_sync_read_failure_logs_first_unresponsive_motor(self):
        class FakeReader:
            start_address = 56
            data_length = 2
            data_dict = {1: [0, 0], 2: [0, 0], 3: [], 4: [], 5: [], 6: []}

            def isAvailable(self, motor_id, _address, _length):
                return bool(self.data_dict[motor_id])

        bus = Mock()
        bus.motors = {
            name: SimpleNamespace(id=index + 1)
            for index, name in enumerate(JOINTS)
        }
        bus.sync_reader = FakeReader()
        bus.sync_read.side_effect = ConnectionError("There is no status packet!")
        self.manager.leader = Mock(bus=bus)

        with (
            self.assertLogs("arms", level="WARNING") as logs,
            self.assertRaises(ConnectionError),
        ):
            self.manager.read_leader_action()

        details = "\n".join(logs.output)
        self.assertIn("已响应=['shoulder_pan(id=1)', 'shoulder_lift(id=2)']", details)
        self.assertIn("首个未响应=elbow_flex(id=3)", details)
        self.assertIn("其后未检查=['wrist_flex(id=4)'", details)

    def test_gripper_current_read_uses_three_retries(self):
        bus = Mock()
        bus.sync_read.return_value = {"gripper": -12}
        self.manager.follower = Mock(bus=bus)

        self.assertEqual(self.manager.read_gripper_current_ma(), 78.0)
        bus.sync_read.assert_called_once_with(
            "Present_Current",
            motors=["gripper"],
            normalize=False,
        )

    def test_estop_protection_is_logged(self):
        self.manager.follower = Mock()
        self.manager.torque_on = True

        with self.assertLogs("arms", level="WARNING") as logs:
            self.manager.estop()

        self.assertFalse(self.manager.torque_on)
        self.manager.follower.bus.disable_torque.assert_called_once_with()
        self.assertTrue(any("触发急停保护" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
