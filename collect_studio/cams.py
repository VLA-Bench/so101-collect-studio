"""相机管理:AVFoundation 枚举(排除内置)、uniqueID↔index 映射、640x480 采集线程、JPEG 预览。

结论(本机实测):AVFoundation 的枚举顺序 = 外置 UVC 相机在前、内置相机在后,
与 OpenCV CAP_AVFOUNDATION 的 index 一一对应;uniqueID 内含 USB 拓扑位置,
接线不动就不变 → 用 uniqueID 持久化,启动时按枚举顺序反查 index。
"""
import logging
import threading

import cv2

from . import config_store

log = logging.getLogger("cams")

ROLES = ["wrist", "left_rear", "right_rear"]


def enumerate_cameras() -> list[dict]:
    """按 OpenCV index 顺序枚举全部视频设备。

    内置/外置用 API 层 deviceType 判断(AVCaptureDeviceTypeExternal* 为外置),
    绝不用本地化的名称/modelID 字符串猜。builtin 只是"默认不选"的提示,
    不限制绑定 —— 误判也只影响默认值,用户永远可以手动改。
    """
    import AVFoundation as AVF

    ext_types = {
        str(t) for t in (
            getattr(AVF, n, None)
            for n in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeExternalUnknown")
        ) if t is not None
    }
    devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)
    out = []
    for i, d in enumerate(devices):
        dtype = str(d.deviceType())
        out.append({
            "index": i,
            "unique_id": str(d.uniqueID()),
            "name": str(d.localizedName()),
            "model": str(d.modelID()),
            "device_type": dtype,
            "builtin": dtype not in ext_types,
        })
    return out


from .avf_capture import AVFCamStream as CamStream  # noqa: E402  按 uniqueID 直采,与 index 无关


class CamManager:
    def __init__(self):
        self.streams: dict[str, CamStream] = {}  # unique_id -> stream
        self.lock = threading.RLock()
        self._devices: list[dict] = []

    def devices(self, refresh: bool = False) -> list[dict]:
        """返回相机快照;只有显式重新识别时才反复触发 AVFoundation 枚举。"""
        with self.lock:
            cached = [dict(d) for d in self._devices]
        if cached and not refresh:
            return cached
        devices = enumerate_cameras()  # 很慢,不能持锁阻塞 /api/status
        with self.lock:
            self._devices = devices
            return [dict(d) for d in devices]

    # ---------- 绑定 ----------
    def ensure_default_binding(self):
        """默认绑定:非内置相机按枚举顺序 → wrist=0 / left_rear=1 / right_rear=2。"""
        cfg = config_store.load()
        cams = cfg["cameras"]
        if all(cams.get(r) for r in ROLES):
            return cams
        ext = [c for c in self.devices(refresh=True) if not c["builtin"]]
        for i, role in enumerate(ROLES):
            if not cams.get(role) and i < len(ext):
                cams[role] = ext[i]["unique_id"]
        config_store.update("cameras", cams)
        return cams

    def bind(self, role: str, unique_id: str):
        if role not in ROLES:
            raise ValueError(f"未知角色 {role}")
        cams = config_store.load()["cameras"]
        for r, uid in list(cams.items()):
            if uid == unique_id and r != role:
                cams[r] = None  # 同一相机不能占两个角色
        cams[role] = unique_id
        config_store.update("cameras", cams)
        return cams

    def unbind(self, role: str):
        if role not in ROLES:
            raise ValueError(f"未知角色 {role}")
        cams = config_store.load()["cameras"]
        cams[role] = None
        config_store.update("cameras", cams)
        return cams

    def resolve(self, refresh: bool = False) -> dict:
        """把绑定的 uniqueID 解析为当前 index;返回 {role: device dict|None}。"""
        cfg = config_store.load()["cameras"]
        found = {c["unique_id"]: c for c in self.devices(refresh=refresh)}
        return {role: found.get(cfg.get(role)) if cfg.get(role) else None for role in ROLES}

    # ---------- 流 ----------
    def _sync_stream(self, uid: str, rec: dict):
        """确保 uid 的流存在;首帧失败或运行中断流都重建。"""
        s = self.streams.get(uid)
        if s and not s.ok and s.err:
            s.stop()
            del self.streams[uid]
            s = None
        if s is None:
            self.streams[uid] = CamStream(uid, rec["width"], rec["height"], rec["fps"])

    def start_all(self, include_builtin: bool = False):
        """打开枚举到的相机供绑定页预览。

        默认跳过内置相机 —— 不占用设备、不点亮摄像头指示灯。只有当内置相机
        已被手动绑定到角色时才照常打开(说明是有意使用/识别纠错后的结果)。
        需要临时查看某台内置相机画面时走 start_uid 按需单开。
        """
        rec = config_store.load()["record"]
        bound = {uid for uid in config_store.load()["cameras"].values() if uid}
        devices = self.devices(refresh=True)
        with self.lock:
            for c in devices:
                if c["builtin"] and not include_builtin and c["unique_id"] not in bound:
                    continue
                self._sync_stream(c["unique_id"], rec)

    def start_uid(self, uid: str):
        """按需打开单台相机(供内置相机手动预览:识别存疑时先看画面再决定绑定)。"""
        rec = config_store.load()["record"]
        if uid not in {d["unique_id"] for d in self.devices()}:
            raise ValueError("设备未检测到,请先『重新识别相机』")
        with self.lock:
            self._sync_stream(uid, rec)
        return self.status()

    def start_bound(self):
        """只打开绑定了角色的三台相机(采集时用,减少带宽)。"""
        rec = config_store.load()["record"]
        resolved = self.resolve()
        with self.lock:
            keep = {d["unique_id"] for d in resolved.values() if d}
            for uid, s in list(self.streams.items()):
                if uid not in keep:
                    s.stop()
                    del self.streams[uid]
            for d in resolved.values():
                if d:
                    self._sync_stream(d["unique_id"], rec)

    def retry_failed_bound(self):
        """仅重启已明确报错的绑定流,供启动阶段做一次自动恢复。"""
        rec = config_store.load()["record"]
        resolved = self.resolve()
        with self.lock:
            for d in resolved.values():
                if d:
                    self._sync_stream(d["unique_id"], rec)

    def bound_health(self) -> dict:
        """返回三路绑定流的真实就绪状态和可直接展示的失败原因。"""
        cfg = config_store.load()["cameras"]
        found = {d["unique_id"] for d in self.devices()}
        problems = {}
        ready = 0
        with self.lock:
            for role in ROLES:
                uid = cfg.get(role)
                if not uid:
                    problems[role] = "未绑定"
                elif uid not in found:
                    problems[role] = "设备未检测到"
                else:
                    stream = self.streams.get(uid)
                    if stream and stream.ok:
                        ready += 1
                    else:
                        problems[role] = (stream.err if stream else None) or "等待首帧"
        return {"ready": ready, "total": len(ROLES), "problems": problems}

    def stream_for_role(self, role: str) -> CamStream | None:
        uid = config_store.load()["cameras"].get(role)
        return self.streams.get(uid) if uid else None

    def stream_for_uid(self, uid: str) -> CamStream | None:
        return self.streams.get(uid)

    def status(self) -> dict:
        cfg = config_store.load()["cameras"]
        devs = self.devices()
        role_of = {uid: r for r, uid in cfg.items() if uid}
        for d in devs:
            d["role"] = role_of.get(d["unique_id"])
            s = self.streams.get(d["unique_id"])
            d["streaming"] = bool(s and s.ok)
            d["stream_err"] = s.err if s else None
        missing = [r for r, uid in cfg.items() if uid and uid not in {x["unique_id"] for x in devs}]
        return {"devices": devs, "binding": cfg, "missing": missing, "bound_health": self.bound_health()}

    def stop_all(self):
        with self.lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()
