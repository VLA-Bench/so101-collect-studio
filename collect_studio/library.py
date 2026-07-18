"""任务 / 批次 / episode 管理。

目录:
  staging/<session>/<ep_id>/          录制暂存
  library/<task_slug>/<session>/<ep_id>/   已保存(parquet + mp4 + meta.json)
  trash/<task_slug>/<session>/<ep_id>/     回收站
"""
import json
import re
import shutil
import threading
import time
from datetime import datetime

from .paths import COUNTER_JSON, LIBRARY, STAGING, TASKS_JSON, TRASH

_LOCK = threading.Lock()


# ---------- 任务 ----------
def load_tasks() -> list[dict]:
    if TASKS_JSON.is_file():
        return json.loads(TASKS_JSON.read_text())
    tasks = [{"slug": "grab_cube", "prompt": "Grab the cube"}]
    TASKS_JSON.write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
    return tasks


def add_task(prompt: str, slug: str | None = None) -> dict:
    with _LOCK:
        tasks = load_tasks()
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "_", prompt.lower()).strip("_") or f"task_{len(tasks)}"
        if any(t["slug"] == slug for t in tasks):
            raise ValueError(f"任务 {slug} 已存在")
        t = {"slug": slug, "prompt": prompt}
        tasks.append(t)
        TASKS_JSON.write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
        return t


# ---------- episode 编号 ----------
def next_episode_id() -> str:
    with _LOCK:
        n = 0
        if COUNTER_JSON.is_file():
            n = json.loads(COUNTER_JSON.read_text())["next"]
        COUNTER_JSON.write_text(json.dumps({"next": n + 1}))
        return f"episode_{n:06d}"


# ---------- 批次 ----------
def new_session_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M")


# ---------- episode 列表 ----------
def _scan(root, status):
    eps = []
    if not root.is_dir():
        return eps
    for meta_file in root.glob("*/*/*/meta.json"):
        try:
            m = json.loads(meta_file.read_text())
            m["status"] = status
            m["dir"] = str(meta_file.parent)
            eps.append(m)
        except Exception:  # noqa: BLE001
            continue
    return eps


def list_episodes() -> list[dict]:
    eps = _scan(LIBRARY, "saved") + _scan(TRASH, "trash")
    eps.sort(key=lambda m: m["id"])
    return eps


def find_episode(ep_id: str) -> dict | None:
    for m in list_episodes():
        if m["id"] == ep_id:
            return m
    return None


def move_episode(ep_id: str, to_trash: bool) -> dict:
    m = find_episode(ep_id)
    if not m:
        raise FileNotFoundError(ep_id)
    src_root, dst_root = (LIBRARY, TRASH) if to_trash else (TRASH, LIBRARY)
    rel = m["dir"].split(str(src_root) + "/", 1)
    if len(rel) != 2:
        raise RuntimeError(f"{ep_id} 已在目标位置")
    dst = dst_root / rel[1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(m["dir"], dst)
    return {"id": ep_id, "status": "trash" if to_trash else "saved"}


def empty_trash() -> int:
    n = len(_scan(TRASH, "trash"))
    if TRASH.is_dir():
        shutil.rmtree(TRASH)
    TRASH.mkdir(parents=True, exist_ok=True)
    return n


def recover_staging() -> list[str]:
    """启动时清点 staging 里的残留(崩溃遗留),返回目录列表供前端提示。"""
    leftovers = []
    if STAGING.is_dir():
        for d in STAGING.glob("*/*"):
            if d.is_dir():
                leftovers.append(str(d))
    return leftovers


def wipe_staging_dir(path: str):
    p = STAGING / path if not path.startswith("/") else None
    from pathlib import Path
    p = Path(path)
    if STAGING in p.parents and p.is_dir():
        shutil.rmtree(p)


def stats() -> dict:
    eps = list_episodes()
    saved = [e for e in eps if e["status"] == "saved"]
    per_task: dict[str, int] = {}
    for e in saved:
        per_task[e["task_slug"]] = per_task.get(e["task_slug"], 0) + 1
    return {
        "saved": len(saved),
        "trash": len(eps) - len(saved),
        "minutes": round(sum(e.get("dur", 0) for e in saved) / 60, 1),
        "per_task": per_task,
    }
