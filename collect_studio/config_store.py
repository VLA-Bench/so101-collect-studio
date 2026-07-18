"""devices.yaml load/save. All hardware identity is persisted by serial number / uniqueID."""
import threading

import yaml

from .paths import DEVICES_YAML

_LOCK = threading.Lock()

DEFAULTS = {
    "arms": {
        # 预置:来自用户已验证的 lerobot-record 命令(可被摆动识别覆盖)
        "leader": {"serial": "5B41532862", "id": "my_awesome_leader_arm"},
        "follower": {"serial": "5B41532784", "id": "my_awesome_follower_arm"},
    },
    # role -> AVFoundation uniqueID;首次启动时按枚举顺序自动填 0/1/2
    "cameras": {"wrist": None, "left_rear": None, "right_rear": None},
    "record": {"fps": 30, "width": 640, "height": 480},
}


_cache: dict = {"mtime": None, "cfg": None}


def load() -> dict:
    """带 mtime 缓存:录制热循环每帧都会调用,不能每次读盘解析 YAML。"""
    with _LOCK:
        mtime = DEVICES_YAML.stat().st_mtime if DEVICES_YAML.is_file() else None
        if _cache["cfg"] is not None and _cache["mtime"] == mtime:
            return _cache["cfg"]
        cfg = {}
        if DEVICES_YAML.is_file():
            cfg = yaml.safe_load(DEVICES_YAML.read_text()) or {}
        # 深合并默认值
        out = {k: dict(v) for k, v in DEFAULTS.items()}
        for k, v in cfg.items():
            if isinstance(v, dict) and k in out:
                out[k].update(v)
            else:
                out[k] = v
        _cache.update(mtime=mtime, cfg=out)
        return out


def save(cfg: dict) -> None:
    with _LOCK:
        DEVICES_YAML.parent.mkdir(parents=True, exist_ok=True)
        DEVICES_YAML.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


def update(section: str, values: dict) -> dict:
    cfg = load()
    cfg.setdefault(section, {}).update(values)
    save(cfg)
    return cfg
