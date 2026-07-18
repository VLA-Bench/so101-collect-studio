"""机械臂管理:串口序列号识别、摆动识别、校准导入/体检、连接、力矩控制。

安全约定:
- 本模块绝不调用 robot.calibrate()(舵机重校准)。connect 一律 calibrate=False。
- 连接后立即 disable_torque,只有显式 start_teleop 才 enable。
"""
import json
import logging
import shutil
import threading
import time

from serial.tools import list_ports

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig
from lerobot.robots.so_follower.so_follower import SOFollower
from lerobot.teleoperators.so_leader.config_so_leader import SOLeaderTeleopConfig
from lerobot.teleoperators.so_leader.so_leader import SOLeader

from . import config_store
from .paths import CALIB_BACKUP, LEROBOT_CALIB

log = logging.getLogger("arms")

ARM_VID = 6790  # 0x1A86 QinHeng (两臂控制板同款)
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def _bare_bus(port: str) -> FeetechMotorsBus:
    return FeetechMotorsBus(
        port=port,
        motors={n: Motor(i + 1, "sts3215", MotorNormMode.RANGE_M100_100) for i, n in enumerate(JOINTS)},
    )


def scan_ports() -> list[dict]:
    """列出候选机械臂串口(按 VID 过滤),附带序列号与当前绑定角色。"""
    cfg = config_store.load()["arms"]
    out = []
    for p in list_ports.comports():
        if p.vid != ARM_VID:
            continue
        role = None
        for r in ("leader", "follower"):
            if cfg.get(r, {}).get("serial") and str(p.serial_number or "").startswith(cfg[r]["serial"][:10]):
                role = r
        out.append({
            "device": p.device.replace("/dev/cu.", "/dev/tty."),
            "serial": p.serial_number,
            "role": role,
        })
    return out


class ArmManager:
    def __init__(self):
        self.follower: SOFollower | None = None
        self.leader: SOLeader | None = None
        self.torque_on = False
        self.lock = threading.RLock()
        self.wiggle: dict = {"state": "idle"}  # idle|running|done|error
        self.last_error: str | None = None

    # ---------- 状态 ----------
    def status(self) -> dict:
        ports = scan_ports()
        cfg = config_store.load()["arms"]
        return {
            "ports": ports,
            "binding": cfg,
            "connected": {
                "leader": self.leader is not None,
                "follower": self.follower is not None,
            },
            "torque_on": self.torque_on,
            "wiggle": self.wiggle,
            "calib_imported": self._calib_files_ok(cfg),
            "error": self.last_error,
        }

    @staticmethod
    def _calib_files_ok(cfg) -> bool:
        f = LEROBOT_CALIB / "robots" / "so_follower" / f"{cfg['follower']['id']}.json"
        l = LEROBOT_CALIB / "teleoperators" / "so_leader" / f"{cfg['leader']['id']}.json"
        return f.is_file() and l.is_file()

    @staticmethod
    def _port_for_serial(serial_prefix: str) -> str | None:
        for p in list_ports.comports():
            if p.vid == ARM_VID and str(p.serial_number or "").startswith(serial_prefix[:10]):
                return p.device.replace("/dev/cu.", "/dev/tty.")
        return None

    # ---------- 摆动识别 ----------
    def wiggle_identify(self, seconds: float = 6.0):
        """后台线程:同时读所有候选口的关节位置,位置在动的即 leader。"""
        if self.wiggle.get("state") == "running":
            return
        if self.follower or self.leader:
            self.wiggle = {"state": "error", "msg": "请先断开机械臂连接再做摆动识别"}
            return
        self.wiggle = {"state": "running", "progress": 0, "msg": "请晃动主动臂…"}
        threading.Thread(target=self._wiggle_worker, args=(seconds,), daemon=True).start()

    def _wiggle_worker(self, seconds: float):
        ports = [p["device"] for p in scan_ports()]
        if len(ports) != 2:
            self.wiggle = {"state": "error", "msg": f"检测到 {len(ports)} 个候选串口,需要恰好 2 个"}
            return
        buses, samples = {}, {p: [] for p in ports}
        try:
            for p in ports:
                b = _bare_bus(p)
                b.connect(handshake=True)
                buses[p] = b
            t0 = time.time()
            while time.time() - t0 < seconds:
                for p, b in buses.items():
                    pos = b.sync_read("Present_Position", normalize=False)
                    samples[p].append(list(pos.values()))
                self.wiggle["progress"] = int((time.time() - t0) / seconds * 100)
                time.sleep(0.05)
            move = {}
            for p, rows in samples.items():
                cols = list(zip(*rows))
                move[p] = sum(max(c) - min(c) for c in cols)
            moving = max(move, key=move.get)
            still = min(move, key=move.get)
            if move[moving] < 40:
                self.wiggle = {"state": "error", "msg": "两个口都没检测到运动,请确认在晃动主动臂"}
                return
            serials = {p["device"]: p["serial"] for p in scan_ports()}
            cfg = config_store.load()
            cfg["arms"]["leader"]["serial"] = serials[moving]
            cfg["arms"]["follower"]["serial"] = serials[still]
            config_store.save(cfg)
            self.wiggle = {
                "state": "done", "progress": 100,
                "msg": f"识别完成:{serials[moving]} = 主动臂(位移 {move[moving]:.0f} ticks),已写入 devices.yaml",
                "leader_serial": serials[moving], "follower_serial": serials[still],
                "movement": {serials[k]: round(v, 1) for k, v in move.items()},
            }
        except Exception as e:  # noqa: BLE001
            log.exception("wiggle failed")
            self.wiggle = {"state": "error", "msg": f"识别失败:{e}"}
        finally:
            for b in buses.values():
                try:
                    b.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 一键校准导入 + 体检 ----------
    def import_calibration(self) -> dict:
        cfg = config_store.load()["arms"]
        copied = []
        pairs = [
            (CALIB_BACKUP / "robots" / "so_follower", LEROBOT_CALIB / "robots" / "so_follower", cfg["follower"]["id"]),
            (CALIB_BACKUP / "teleoperators" / "so_leader", LEROBOT_CALIB / "teleoperators" / "so_leader", cfg["leader"]["id"]),
        ]
        for src_dir, dst_dir, arm_id in pairs:
            srcs = list(src_dir.glob("*.json"))
            if not srcs:
                raise FileNotFoundError(f"备份缺失:{src_dir}")
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / f"{arm_id}.json"
            shutil.copy2(srcs[0], dst)
            copied.append(str(dst))
        return {"copied": copied}

    def health_check(self) -> dict:
        """只读体检:临时连接两臂总线,读当前位置,对照校准行程。不动任何舵机。"""
        cfg = config_store.load()["arms"]
        report = {}
        for role in ("follower", "leader"):
            arm_id = cfg[role]["id"]
            sub = "robots/so_follower" if role == "follower" else "teleoperators/so_leader"
            calib_file = LEROBOT_CALIB / sub / f"{arm_id}.json"
            if not calib_file.is_file():
                report[role] = {"ok": False, "msg": "校准文件缺失,请先一键导入"}
                continue
            calib = json.loads(calib_file.read_text())
            port = self._port_for_serial(cfg[role]["serial"])
            if not port:
                report[role] = {"ok": False, "msg": "串口未找到,请检查连线"}
                continue
            with self.lock:
                bus = self._existing_bus(role)
                own = bus is None
                try:
                    if own:
                        bus = _bare_bus(port)
                        bus.connect(handshake=True)
                    raw = bus.sync_read("Present_Position", normalize=False)
                    joints = []
                    all_ok = True
                    for name, ticks in raw.items():
                        c = calib.get(name, {})
                        ok = c.get("range_min", 0) - 60 <= ticks <= c.get("range_max", 4095) + 60
                        all_ok &= ok
                        joints.append({
                            "name": name, "ticks": int(ticks), "ok": bool(ok),
                            "homing_offset": c.get("homing_offset"),
                            "range": [c.get("range_min"), c.get("range_max")],
                        })
                    report[role] = {"ok": all_ok, "joints": joints, "port": port}
                except Exception as e:  # noqa: BLE001
                    report[role] = {"ok": False, "msg": f"总线通讯失败:{e}"}
                finally:
                    if own and bus is not None:
                        try:
                            bus.disconnect()
                        except Exception:  # noqa: BLE001
                            pass
        return report

    def _existing_bus(self, role):
        if role == "follower" and self.follower:
            return self.follower.bus
        if role == "leader" and self.leader:
            return self.leader.bus
        return None

    # ---------- 连接 / 断开 ----------
    @staticmethod
    def _connect_with_retry(make_dev, tries: int = 3):
        """connect() 的 configure 阶段逐关节写配置,Feetech 总线偶发丢应答包会让
        整次连接失败(上游不重试)—— 这里整体重试,瞬时丢包重连即可恢复。"""
        last = None
        for i in range(tries):
            dev = make_dev()
            try:
                dev.connect(calibrate=False)
                return dev
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("connect attempt %d/%d failed: %s", i + 1, tries, e)
                try:
                    if dev.bus.is_connected:
                        dev.bus.disconnect(disable_torque=True)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5)
        raise RuntimeError(f"连接失败(重试 {tries} 次):{last}")

    def connect(self) -> dict:
        with self.lock:
            cfg = config_store.load()["arms"]
            self.last_error = None
            try:
                if not self.leader:
                    port = self._port_for_serial(cfg["leader"]["serial"])
                    if not port:
                        raise RuntimeError("主动臂串口未找到")
                    self.leader = self._connect_with_retry(
                        lambda: SOLeader(SOLeaderTeleopConfig(port=port, id=cfg["leader"]["id"])))
                if not self.follower:
                    fport = self._port_for_serial(cfg["follower"]["serial"])
                    if not fport:
                        raise RuntimeError("从动臂串口未找到")
                    self.follower = self._connect_with_retry(
                        lambda: SOFollower(SOFollowerRobotConfig(port=fport, id=cfg["follower"]["id"])))
                    # 连接后 configure() 会打开力矩 —— 立刻释放,等显式遥操作再上力矩
                    self.follower.bus.disable_torque(num_retry=3)
                    self.torque_on = False
            except Exception as e:  # noqa: BLE001
                log.exception("connect failed")
                self.last_error = str(e)
                self.disconnect()
                raise
            return {"ok": True}

    def disconnect(self):
        with self.lock:
            for attr in ("follower", "leader"):
                dev = getattr(self, attr)
                if dev:
                    try:
                        dev.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    setattr(self, attr, None)
            self.torque_on = False

    # ---------- 力矩 ----------
    def enable_torque(self):
        with self.lock:
            if not self.follower:
                raise RuntimeError("从动臂未连接")
            self.follower.bus.enable_torque()
            self.torque_on = True

    def estop(self):
        """急停:从动臂断力矩。任何状态下可调用。"""
        with self.lock:
            self.torque_on = False
            if self.follower:
                try:
                    self.follower.bus.disable_torque()
                except Exception:  # noqa: BLE001
                    log.exception("estop disable_torque failed")
