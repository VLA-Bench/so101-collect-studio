"""相机管理:AVFoundation 枚举(排除内置)、uniqueID↔index 映射、640x480 采集线程、JPEG 预览。

结论(本机实测):AVFoundation 的枚举顺序 = 外置 UVC 相机在前、内置相机在后,
与 OpenCV CAP_AVFOUNDATION 的 index 一一对应;uniqueID 内含 USB 拓扑位置,
接线不动就不变 → 用 uniqueID 持久化,启动时按枚举顺序反查 index。
"""
import logging
import threading
import time

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
        self.lock = threading.Lock()

    # ---------- 绑定 ----------
    def ensure_default_binding(self):
        """默认绑定:非内置相机按枚举顺序 → wrist=0 / left_rear=1 / right_rear=2。"""
        cfg = config_store.load()
        cams = cfg["cameras"]
        if all(cams.get(r) for r in ROLES):
            return cams
        ext = [c for c in enumerate_cameras() if not c["builtin"]]
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

    def resolve(self) -> dict:
        """把绑定的 uniqueID 解析为当前 index;返回 {role: device dict|None}。"""
        cfg = config_store.load()["cameras"]
        found = {c["unique_id"]: c for c in enumerate_cameras()}
        return {role: found.get(uid) if uid else None for role, uid in cfg.items()}

    # ---------- 流 ----------
    def _sync_stream(self, uid: str, rec: dict):
        """确保 uid 的流存在;死流(从未出帧且报错)重建 —— 拔插后点「重新识别」即可复活。"""
        s = self.streams.get(uid)
        if s and not s.ok and s.frame_count == 0 and s.err:
            s.stop()
            del self.streams[uid]
            s = None
        if s is None:
            self.streams[uid] = CamStream(uid, rec["width"], rec["height"], rec["fps"])

    def start_all(self):
        """打开全部枚举到的相机(含内置,供绑定页预览用)。"""
        rec = config_store.load()["record"]
        with self.lock:
            for c in enumerate_cameras():
                self._sync_stream(c["unique_id"], rec)

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

    def stream_for_role(self, role: str) -> CamStream | None:
        uid = config_store.load()["cameras"].get(role)
        return self.streams.get(uid) if uid else None

    def stream_for_uid(self, uid: str) -> CamStream | None:
        return self.streams.get(uid)

    def status(self) -> dict:
        cfg = config_store.load()["cameras"]
        devs = enumerate_cameras()
        role_of = {uid: r for r, uid in cfg.items() if uid}
        for d in devs:
            d["role"] = role_of.get(d["unique_id"])
            s = self.streams.get(d["unique_id"])
            d["streaming"] = bool(s and s.ok)
            d["stream_err"] = s.err if s else None
        missing = [r for r, uid in cfg.items() if uid and uid not in {x["unique_id"] for x in devs}]
        return {"devices": devs, "binding": cfg, "missing": missing}

    def stop_all(self):
        with self.lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()
