"""macOS UVC 控件:IOKit DeviceRequest 走 endpoint-0,不抢 AVFoundation 的流。

实测(2026-08,三路同时出图):
- PyUSB/libusb GET/SET 一律 Errno 13(Access denied),因为 Apple UVC 驱动已占设备。
- IOKit USBDeviceOpen + DeviceRequest 可按 uniqueID 独立读写,不断流。
- 两台 Realtek 0x0bda:0x1376 的 USB 序列号也相同,必须按 locationID 寻址。
- AVFoundation uniqueID = hex((locationID << 32) | (vid << 16) | pid)。
- 这三台 AE 只接受 1(manual) 与 8(aperture_priority);标准 auto=2 会 stall。
- Realtek 的 gain 描述符标了支持,读写 stall;色温描述符没标,但能读写。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_int,
    c_int32,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
    cast,
    create_string_buffer,
)
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("uvc_controls")

_LOCK = threading.RLock()

SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84
GET_DEF = 0x87
BM_GET = 0xA1
BM_SET = 0x21

# 这三台外置 UVC 实测只吃这两档
AE_MANUAL = 0x01
AE_APERTURE = 0x08

# 0xe000404f = iokit_usb_err(0x4f) kIOUSBPipeStalled
_STALL = ctypes.c_int32(0xE000404F).value


@dataclass(frozen=True)
class Control:
    name: str
    entity: str  # "ct" / "pu"
    selector: int
    size: int
    signed: bool = False


# 前端会用到的控件。增益/色温按 GET_CUR 是否成功决定是否展示。
CONTROLS: dict[str, Control] = {
    "auto_exposure_mode": Control("auto_exposure_mode", "ct", 0x02, 1),
    "absolute_exposure_time": Control("absolute_exposure_time", "ct", 0x04, 4),
    "brightness": Control("brightness", "pu", 0x02, 2, signed=True),
    "contrast": Control("contrast", "pu", 0x03, 2),
    "gain": Control("gain", "pu", 0x04, 2),
    "saturation": Control("saturation", "pu", 0x07, 2),
    "white_balance_temperature": Control("white_balance_temperature", "pu", 0x0A, 2),
    "auto_white_balance_temperature": Control("auto_white_balance_temperature", "pu", 0x0B, 1),
}

# 写入 devices.yaml / 前端 PATCH 的友好键
SAVED_KEYS = (
    "auto_exposure",
    "absolute_exposure_time",
    "brightness",
    "contrast",
    "saturation",
    "gain",
    "auto_white_balance",
    "white_balance_temperature",
)


class UVCError(RuntimeError):
    pass


def unique_id_from_usb(location_id: int, vid: int, pid: int) -> str:
    """与 AVFoundation 外置 UVC uniqueID 同一套编码。"""
    return hex(((location_id & 0xFFFFFFFF) << 32) | ((vid & 0xFFFF) << 16) | (pid & 0xFFFF))


def location_id_from_unique_id(unique_id: str) -> int | None:
    """从 AVFoundation uniqueID 解 USB locationID;解不出返回 None。"""
    text = (unique_id or "").strip()
    if not text.lower().startswith("0x"):
        return None
    try:
        n = int(text, 16)
    except ValueError:
        return None
    if n <= 0:
        return None
    loc = (n >> 32) & 0xFFFFFFFF
    if loc:
        return loc
    # 少数设备 uniqueID 就是 locationID 本身
    if 0x1000 <= n <= 0xFFFFFFFF:
        return n
    return None


def decode_auto_exposure(raw: int) -> bool:
    """非 manual(1) 一律视为自动(这三台自动档是 8)。"""
    return raw != AE_MANUAL


def encode_auto_exposure(enabled: bool) -> int:
    return AE_APERTURE if enabled else AE_MANUAL


def snapshot_to_saved(snapshot: dict) -> dict:
    """从 read/set 快照抽出可持久化的友好键(只保留 supported)。"""
    out = {}
    controls = snapshot.get("controls") or {}
    ae = controls.get("auto_exposure_mode")
    if ae and ae.get("supported"):
        out["auto_exposure"] = bool(ae.get("auto"))
    awb = controls.get("auto_white_balance_temperature")
    if awb and awb.get("supported"):
        out["auto_white_balance"] = bool(awb.get("on"))
    for key in (
        "absolute_exposure_time",
        "brightness",
        "contrast",
        "saturation",
        "gain",
        "white_balance_temperature",
    ):
        item = controls.get(key)
        if item and item.get("supported") and item.get("value") is not None:
            out[key] = item["value"]
    return out


def pretty_value(name: str, value: int) -> str:
    if name == "absolute_exposure_time":
        return f"{value * 0.1:g} ms"
    if name == "white_balance_temperature":
        return f"{value} K"
    if name == "auto_exposure_mode":
        return "自动" if decode_auto_exposure(value) else "手动"
    if name == "auto_white_balance_temperature":
        return "开" if value else "关"
    return str(value)


# ---- IOKit glue -------------------------------------------------------------

class _CFUUIDBytes(Structure):
    _fields_ = [("bytes", c_uint8 * 16)]


class _IOUSBDevRequest(Structure):
    _fields_ = [
        ("bmRequestType", c_uint8),
        ("bRequest", c_uint8),
        ("wValue", c_uint16),
        ("wIndex", c_uint16),
        ("wLength", c_uint16),
        ("pData", c_void_p),
        ("wLenDone", c_uint32),
    ]


_QUERYINTERFACE = ctypes.CFUNCTYPE(c_int32, c_void_p, _CFUUIDBytes, POINTER(c_void_p))
_HRESULT = ctypes.CFUNCTYPE(c_int32, c_void_p)
_DEVREQ = ctypes.CFUNCTYPE(c_int32, c_void_p, POINTER(_IOUSBDevRequest))
_RELEASE = ctypes.CFUNCTYPE(c_uint32, c_void_p)

_kIOUSBDeviceUserClientTypeID = "9dc7b780-9ec0-11d4-a54f-000a27052861"
_kIOCFPlugInInterfaceID = "C244E858-109C-11D4-91D4-0050E4C6426F"
_kIOUSBDeviceInterfaceID650 = "4AAC1B2E-24C2-476A-964D-91333534F2CC"

_LIBS = None


def _uuid(hex_text: str) -> _CFUUIDBytes:
    h = hex_text.replace("-", "")
    return _CFUUIDBytes((c_uint8 * 16)(*[int(h[i : i + 2], 16) for i in range(0, 32, 2)]))


def _load_iokit():
    global _LIBS
    if _LIBS is not None:
        return _LIBS
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFUUIDCreateFromUUIDBytes.restype = c_void_p
    cf.CFUUIDCreateFromUUIDBytes.argtypes = [c_void_p, _CFUUIDBytes]
    cf.CFUUIDGetUUIDBytes.restype = _CFUUIDBytes
    cf.CFUUIDGetUUIDBytes.argtypes = [c_void_p]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
    cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_ubyte
    cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_ubyte
    cf.CFRelease.argtypes = [c_void_p]
    iokit.IOServiceMatching.restype = c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingServices.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
    iokit.IOIteratorNext.restype = c_void_p
    iokit.IOIteratorNext.argtypes = [c_void_p]
    iokit.IOObjectRelease.argtypes = [c_void_p]
    iokit.IORegistryEntryCreateCFProperty.restype = c_void_p
    iokit.IORegistryEntryCreateCFProperty.argtypes = [c_void_p, c_void_p, c_void_p, c_uint32]
    iokit.IOCreatePlugInInterfaceForService.restype = c_int32
    iokit.IOCreatePlugInInterfaceForService.argtypes = [
        c_void_p, c_void_p, c_void_p, POINTER(c_void_p), POINTER(c_int32),
    ]
    _LIBS = {
        "iokit": iokit,
        "cf": cf,
        "master": c_void_p.in_dll(iokit, "kIOMasterPortDefault"),
        "plugin_type": cf.CFUUIDCreateFromUUIDBytes(None, _uuid(_kIOUSBDeviceUserClientTypeID)),
        "plugin_iid": cf.CFUUIDCreateFromUUIDBytes(None, _uuid(_kIOCFPlugInInterfaceID)),
        "dev_iid": cf.CFUUIDCreateFromUUIDBytes(None, _uuid(_kIOUSBDeviceInterfaceID650)),
    }
    return _LIBS


def _cfstr(text: str):
    return _load_iokit()["cf"].CFStringCreateWithCString(None, text.encode(), 0x08000100)


def _read_num(entry, key: str) -> int | None:
    libs = _load_iokit()
    k = _cfstr(key)
    prop = libs["iokit"].IORegistryEntryCreateCFProperty(entry, k, None, 0)
    libs["cf"].CFRelease(k)
    if not prop:
        return None
    out = c_int32(0)
    ok = libs["cf"].CFNumberGetValue(prop, 3, byref(out))
    libs["cf"].CFRelease(prop)
    return int(out.value) if ok else None


def _vtbl(iface):
    ptr = cast(iface, POINTER(c_void_p)).contents.value
    return cast(ptr, POINTER(c_void_p))


def _iter_usb_services():
    libs = _load_iokit()
    matching = libs["iokit"].IOServiceMatching(b"IOUSBHostDevice")
    it = c_void_p()
    kr = libs["iokit"].IOServiceGetMatchingServices(libs["master"], matching, byref(it))
    if kr != 0 or not it:
        return
    try:
        while True:
            svc = libs["iokit"].IOIteratorNext(it)
            if not svc:
                break
            yield svc
    finally:
        libs["iokit"].IOObjectRelease(it)


def _find_service(unique_id: str):
    want = (unique_id or "").strip().lower()
    want_loc = location_id_from_unique_id(unique_id)
    libs = _load_iokit()
    found = None
    for svc in _iter_usb_services():
        try:
            vid = _read_num(svc, "idVendor")
            pid = _read_num(svc, "idProduct")
            loc = _read_num(svc, "locationID")
            if vid is None or pid is None or loc is None:
                libs["iokit"].IOObjectRelease(svc)
                continue
            computed = unique_id_from_usb(loc, vid, pid).lower()
            if computed == want or (want_loc is not None and loc == want_loc):
                found = (svc, loc, vid, pid)
                break
            libs["iokit"].IOObjectRelease(svc)
        except Exception:  # noqa: BLE001
            libs["iokit"].IOObjectRelease(svc)
            raise
    return found


def _open_handle(unique_id: str) -> dict:
    found = _find_service(unique_id)
    if not found:
        raise UVCError("未找到对应的 UVC 设备(内置相机或不支持)")
    svc, loc, vid, pid = found
    libs = _load_iokit()
    plugin = c_void_p()
    score = c_int32()
    try:
        kr = libs["iokit"].IOCreatePlugInInterfaceForService(
            svc, libs["plugin_type"], libs["plugin_iid"], byref(plugin), byref(score),
        )
    finally:
        libs["iokit"].IOObjectRelease(svc)
    if kr != 0 or not plugin:
        raise UVCError(f"无法打开 USB 插件(kr=0x{kr & 0xffffffff:x})")
    dev = c_void_p()
    try:
        qi = _QUERYINTERFACE(_vtbl(plugin)[1])
        uuid_bytes = libs["cf"].CFUUIDGetUUIDBytes(libs["dev_iid"])
        hr = qi(plugin, uuid_bytes, byref(dev))
    finally:
        _RELEASE(_vtbl(plugin)[3])(plugin)
    if hr != 0 or not dev:
        raise UVCError(f"QueryInterface 失败(hr=0x{hr & 0xffffffff:x})")
    kr = _HRESULT(_vtbl(dev)[8])(dev)  # USBDeviceOpen
    if ctypes.c_int32(kr).value != 0:
        _RELEASE(_vtbl(dev)[3])(dev)
        raise UVCError(f"USBDeviceOpen 失败(kr=0x{kr & 0xffffffff:x})")
    return {"dev": dev, "location_id": loc, "vid": vid, "pid": pid}


def _close_handle(handle: dict) -> None:
    dev = handle.get("dev")
    if not dev:
        return
    try:
        _HRESULT(_vtbl(dev)[9])(dev)  # USBDeviceClose
    except Exception:  # noqa: BLE001
        log.exception("USBDeviceClose failed")
    try:
        _RELEASE(_vtbl(dev)[3])(dev)
    except Exception:  # noqa: BLE001
        log.exception("USB device Release failed")


def _iokit_xfer(handle: dict, bm: int, request: int, entity: int, selector: int,
                size: int, payload: bytes | None = None) -> tuple[int, bytes]:
    buf = create_string_buffer(size)
    if payload is not None:
        raw = payload[:size].ljust(size, b"\x00")
        buf.raw = raw
    req = _IOUSBDevRequest()
    req.bmRequestType = bm
    req.bRequest = request
    req.wValue = selector << 8
    req.wIndex = (entity << 8) | 0
    req.wLength = size
    req.pData = cast(buf, c_void_p)
    req.wLenDone = 0
    kr = _DEVREQ(_vtbl(handle["dev"])[26])(handle["dev"], byref(req))
    n = req.wLenDone or size
    return ctypes.c_int32(kr).value, bytes(buf.raw[:n])


XferFn = Callable[[int, int, int, int, int, bytes | None], tuple[int, bytes]]


def _with_device(unique_id: str, fn: Callable[[XferFn], object]):
    """打开设备跑 fn(xfer);测试可 patch 本函数。"""
    with _LOCK:
        handle = _open_handle(unique_id)
        try:
            def xfer(bm, request, entity, selector, size, payload=None):
                return _iokit_xfer(handle, bm, request, entity, selector, size, payload)
            return fn(xfer)
        finally:
            _close_handle(handle)


def _entity_id(control: Control) -> int:
    return 1 if control.entity == "ct" else 2


def _decode(raw: bytes, control: Control) -> int:
    data = raw[: control.size].ljust(control.size, b"\x00")
    return int.from_bytes(data, "little", signed=control.signed)


def _encode(value: int, control: Control) -> bytes:
    bits = control.size * 8
    if control.signed:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    else:
        lo, hi = 0, (1 << bits) - 1
    if value < lo or value > hi:
        raise UVCError(f"{control.name}={value} 超出编码范围 [{lo}, {hi}]")
    return int(value).to_bytes(control.size, "little", signed=control.signed)


def _get_value(xfer: XferFn, control: Control, request: int = GET_CUR) -> int | None:
    kr, raw = xfer(BM_GET, request, _entity_id(control), control.selector, control.size, None)
    if kr != 0:
        return None
    try:
        return _decode(raw, control)
    except Exception:  # noqa: BLE001
        return None


def _set_value(xfer: XferFn, control: Control, value: int) -> None:
    payload = _encode(value, control)
    kr, _ = xfer(BM_SET, SET_CUR, _entity_id(control), control.selector, control.size, payload)
    if kr != 0:
        raise UVCError(f"设置 {control.name}={value} 失败(kr=0x{kr & 0xffffffff:x})")


def _control_item(xfer: XferFn, control: Control) -> dict:
    cur = _get_value(xfer, control, GET_CUR)
    supported = cur is not None
    item = {
        "name": control.name,
        "supported": supported,
        "value": cur,
        "min": _get_value(xfer, control, GET_MIN) if supported else None,
        "max": _get_value(xfer, control, GET_MAX) if supported else None,
        "res": _get_value(xfer, control, GET_RES) if supported else None,
        "default": _get_value(xfer, control, GET_DEF) if supported else None,
        "pretty": pretty_value(control.name, cur) if cur is not None else None,
    }
    if control.name == "auto_exposure_mode":
        item["auto"] = decode_auto_exposure(cur) if cur is not None else None
    if control.name == "auto_white_balance_temperature":
        item["on"] = bool(cur) if cur is not None else None
    return item


def _read_with_xfer(xfer: XferFn) -> dict:
    controls = {name: _control_item(xfer, ctrl) for name, ctrl in CONTROLS.items()}
    return {"ok": True, "err": None, "controls": controls}


def read_controls(unique_id: str) -> dict:
    loc = location_id_from_unique_id(unique_id)
    try:
        snap = _with_device(unique_id, _read_with_xfer)
    except UVCError as e:
        return {"ok": False, "err": str(e), "controls": {}, "unique_id": unique_id}
    except Exception as e:  # noqa: BLE001
        log.exception("read_controls %s", unique_id)
        return {"ok": False, "err": f"读取失败:{e}", "controls": {}, "unique_id": unique_id}
    snap["unique_id"] = unique_id
    snap["location_id"] = hex(loc) if loc else None
    return snap


def normalize_patch(values: dict) -> dict:
    out = {}
    for key, val in (values or {}).items():
        if key not in SAVED_KEYS or val is None:
            continue
        if key in ("auto_exposure", "auto_white_balance"):
            out[key] = bool(val)
        else:
            out[key] = int(val)
    return out


def _apply_patch(xfer: XferFn, patch: dict) -> None:
    """先关自动再写手动量,最后再开自动,避免 SET stall。"""
    ae = patch.get("auto_exposure")
    awb = patch.get("auto_white_balance")
    if ae is False:
        _set_value(xfer, CONTROLS["auto_exposure_mode"], AE_MANUAL)
    if awb is False:
        _set_value(xfer, CONTROLS["auto_white_balance_temperature"], 0)

    manual_map = {
        "absolute_exposure_time": (ae is not True, CONTROLS["absolute_exposure_time"]),
        "white_balance_temperature": (awb is not True, CONTROLS["white_balance_temperature"]),
        "brightness": (True, CONTROLS["brightness"]),
        "contrast": (True, CONTROLS["contrast"]),
        "saturation": (True, CONTROLS["saturation"]),
        "gain": (True, CONTROLS["gain"]),
    }
    for key, (allow, control) in manual_map.items():
        if key not in patch or not allow:
            continue
        try:
            _set_value(xfer, control, patch[key])
        except UVCError as e:
            if key == "gain":
                log.warning("忽略增益:%s", e)
                continue
            raise

    if ae is True:
        _set_value(xfer, CONTROLS["auto_exposure_mode"], AE_APERTURE)
    if awb is True:
        _set_value(xfer, CONTROLS["auto_white_balance_temperature"], 1)


def set_controls(unique_id: str, values: dict) -> dict:
    patch = normalize_patch(values)
    if not patch:
        return read_controls(unique_id)

    def _do(xfer: XferFn):
        _apply_patch(xfer, patch)
        return _read_with_xfer(xfer)

    loc = location_id_from_unique_id(unique_id)
    try:
        snap = _with_device(unique_id, _do)
    except UVCError as e:
        return {"ok": False, "err": str(e), "controls": {}, "unique_id": unique_id}
    except Exception as e:  # noqa: BLE001
        log.exception("set_controls %s", unique_id)
        return {"ok": False, "err": f"设置失败:{e}", "controls": {}, "unique_id": unique_id}
    snap["unique_id"] = unique_id
    snap["location_id"] = hex(loc) if loc else None
    return snap


def apply_saved(unique_id: str, saved: dict | None, *, auto_exposure: bool | None = None) -> dict:
    """开流后套用已存参数;没有存档时至少把 AE 打到与 AVFoundation 一致。"""
    patch = dict(saved or {})
    if auto_exposure is not None and "auto_exposure" not in patch:
        patch["auto_exposure"] = bool(auto_exposure)
    if not patch:
        return read_controls(unique_id)
    return set_controls(unique_id, patch)
