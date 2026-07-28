"""任务 / 批次 / episode 管理。

目录:
  staging/<session>/<ep_id>/                录制暂存
  library/<task_set>/<task_slug>/<session>/<ep_id>/   已保存(parquet + mp4 + meta.json)
  trash/<task_set>/<task_slug>/<session>/<ep_id>/     回收站

数据身份是 (task_set, task_slug) 二元组:不同任务集合里相同 prompt(相同 slug)
的 episode 落在不同目录,互不合并。<task_set> 直接用集合名做目录名。
"""
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .paths import COUNTER_JSON, DATA_ROOT, LIBRARY, STAGING, TASKS_JSON, TRASH

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


# ---------- 任务 ----------
DEFAULT_SET = "默认"


def _slugify(prompt: str, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", prompt.lower()).strip("_") or fallback


def _set_path(name: str | None) -> Path:
    """任务集合文件:「默认」用 legacy tasks.json,其余为 ~/so101_data/tasks_<name>.json。
    同名 .jsonl 副本也识别(内容可为 LeRobot tasks.jsonl 格式);.json 优先,新写默认 .json。"""
    if name and not re.fullmatch(r"[\w-]+", name):  # 防路径穿越
        raise ValueError(f"非法任务集合名:{name}")
    stem = "tasks" if not name or name == DEFAULT_SET else f"tasks_{name}"
    json_path = DATA_ROOT / f"{stem}.json"
    if json_path.is_file():
        return json_path
    jsonl_path = DATA_ROOT / f"{stem}.jsonl"
    if jsonl_path.is_file():
        return jsonl_path
    return json_path


def list_task_sets() -> list[str]:
    """可用任务集合:legacy tasks.json(或 tasks.jsonl)记为「默认」,加上所有 tasks_<name>.json/.jsonl。"""
    names: list[str] = []
    for p in sorted(DATA_ROOT.glob("tasks_*.json")) + sorted(DATA_ROOT.glob("tasks_*.jsonl")):
        name = p.stem[len("tasks_"):]
        if name not in names:  # 同名 .json 优先,.jsonl 忽略
            names.append(name)
    sets = [DEFAULT_SET] if (TASKS_JSON.is_file() or TASKS_JSON.with_suffix(".jsonl").is_file()) else []
    return sets + names or [DEFAULT_SET]


def _parse_tasks_text(text: str) -> list[dict]:
    """双格式解析:首个非空字符是 `[` → JSON 数组(条目 {"slug","prompt"} 或 {"prompt"});
    否则按 JSONL 逐行解析(LeRobot {"task_index","task"} 或 {"prompt"},均可带可选 "slug")。
    slug 缺省时按代码规则从 prompt 派生;文件里显式写的 slug 原样保留。"""
    s = text.strip()
    items: list = []
    if s.startswith("["):
        data = json.loads(s)
        if not isinstance(data, list):
            raise ValueError("任务文件 JSON 顶层必须是数组")
        items = data
    else:
        for lineno, line in enumerate(s.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"第 {lineno} 行不是合法 JSON") from e
            items.append(obj)
    tasks = []
    for obj in items:
        if not isinstance(obj, dict):
            raise ValueError("任务条目必须是 JSON 对象")
        prompt = str(obj.get("prompt") or obj.get("task") or "").strip()
        if not prompt:
            raise ValueError("任务条目缺少 prompt/task 字段")
        slug = str(obj.get("slug") or "").strip() or _slugify(prompt, f"task_{len(tasks)}")
        tasks.append({"slug": slug, "prompt": prompt})
    return tasks


def _read_set_file(path: Path) -> list[dict]:
    if path.is_file():
        return _parse_tasks_text(path.read_text())
    if path == TASKS_JSON:  # 首次运行:补一条示例任务(沿用原有行为)
        tasks = [{"slug": "grab_cube", "prompt": "Grab the cube"}]
        _write_set_file(path, tasks)
        return tasks
    raise FileNotFoundError(path)


def _write_set_file(path: Path, tasks: list[dict]):
    """按文件扩展名写回:.jsonl → LeRobot 原生行 {"task_index","task"};.json → [{"slug","prompt"}]。"""
    if path.suffix == ".jsonl":
        path.write_text("".join(
            json.dumps({"task_index": i, "task": t["prompt"]}, ensure_ascii=False) + "\n"
            for i, t in enumerate(tasks)))
    else:
        path.write_text(json.dumps(
            [{"slug": t["slug"], "prompt": t["prompt"]} for t in tasks], ensure_ascii=False, indent=1))


def load_tasks(set_name: str | None = None) -> list[dict]:
    """set_name=None 合并全部集合(不跨集合去重:相同 slug 在不同集合是不同任务,各保留一条);
    否则只读该集合。每个任务带 set 字段。任务身份 = (set, slug)。"""
    if set_name:
        return [{**t, "set": set_name} for t in _read_set_file(_set_path(set_name))]
    tasks = []
    for name in list_task_sets():
        tasks.extend({**t, "set": name} for t in _read_set_file(_set_path(name)))
    return tasks


def load_tasks_grouped() -> dict[str, list[dict]]:
    """按集合分组读取(不跨集合去重):前端切换集合时按它过滤——合并列表的去重会把
    与其他集合重复的任务标成先出现的集合,导致该集合在前端过滤后显示为空。"""
    return {name: load_tasks(name) for name in list_task_sets()}


def add_task(prompt: str, slug: str | None = None, set_name: str | None = None) -> dict:
    """新任务写入指定集合文件(默认「默认」集合,不存在则新建);仅在目标集合内按 slug 查重
    (不同集合允许同 slug——数据按 (set, slug) 隔离)。"""
    with _LOCK:
        all_tasks = load_tasks()
        if not slug:
            slug = _slugify(prompt, f"task_{len(all_tasks)}")
        path = _set_path(set_name)
        try:
            tasks = _read_set_file(path)
        except FileNotFoundError:
            tasks = []
        if any(t["slug"] == slug for t in tasks):
            raise ValueError(f"任务 {slug} 已存在")
        t = {"slug": slug, "prompt": prompt}
        tasks.append(t)
        _write_set_file(path, tasks)
        return {**t, "set": set_name or DEFAULT_SET}


def import_tasks(content: str, set_name: str | None = None) -> dict:
    """导入任务内容到指定集合文件(默认「默认」集合):支持 JSON 数组或 tasks.jsonl(识别
    LeRobot meta/tasks.jsonl 的 {"task_index": n, "task": "..."} 或本应用原生 {"prompt": "..."})。
    仅在目标集合内按 slug 去重(重复跳过;不同集合允许同 slug),全部解析成功才落盘。
    返回 {"added", "skipped", "total"}。"""
    parsed = _parse_tasks_text(content)  # 先整体解析,任何一行坏掉都不落盘
    with _LOCK:
        path = _set_path(set_name)
        try:
            own = _read_set_file(path)
        except FileNotFoundError:
            own = []
        added = skipped = 0
        for t in parsed:
            if any(x["slug"] == t["slug"] for x in own):
                skipped += 1
                continue
            own.append(t)
            added += 1
        if added:
            _write_set_file(path, own)
        return {"added": added, "skipped": skipped, "total": len(load_tasks())}


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
    for meta_file in root.glob("*/*/*/*/meta.json"):
        try:
            m = json.loads(meta_file.read_text())
            m["status"] = status
            m["dir"] = str(meta_file.parent)
            # task_set 以目录结构为准(相对 root 第一段),兼容 meta 里没写该字段的旧数据
            m["task_set"] = meta_file.relative_to(root).parts[0]
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


def update_episode(ep_id: str, task_prompt: str | None = None, task_slug: str | None = None,
                   task_set: str | None = None) -> dict:
    """改 episode 的 meta.json:task_prompt 直接改文本;(task_set, task_slug) 变化时校验目标任务
    存在于任务集合(prompt 未给则取该任务的 prompt),并把目录移到 <root>/<set>/<slug>/<session>/<id>。
    task_slug 给了而 task_set 未给时,取合并列表里第一个匹配 slug 的任务所在集合。"""
    with _LOCK:
        m = find_episode(ep_id)
        if not m:
            raise FileNotFoundError(ep_id)
        old_slug = m["task_slug"]
        old_set = m.get("task_set", DEFAULT_SET)
        new_slug = (task_slug or old_slug).strip()
        new_set = (task_set or old_set).strip() or DEFAULT_SET
        if (new_slug, new_set) != (old_slug, old_set):
            if task_set:
                target = next((t for t in load_tasks(new_set) if t["slug"] == new_slug), None)
            else:
                target = next((t for t in load_tasks() if t["slug"] == new_slug), None)
                new_set = target["set"] if target else new_set
            if not target:
                raise ValueError(f"任务 {new_set}/{new_slug} 不存在(请先在采集台创建该任务)")
            if task_prompt is None:
                task_prompt = target["prompt"]
        new_prompt = (task_prompt if task_prompt is not None else m["task_prompt"]).strip()
        if not new_prompt:
            raise ValueError("提示词不能为空")
        d = Path(m["dir"])
        if (new_slug, new_set) != (old_slug, old_set):
            root = LIBRARY if m["status"] == "saved" else TRASH
            dst = root / new_set / new_slug / m["session"] / m["id"]
            if dst.exists():
                raise ValueError(f"目标位置已存在同名 episode:{dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dst))
            d = dst
        meta = json.loads((d / "meta.json").read_text())
        meta["task_prompt"] = new_prompt
        meta["task_slug"] = new_slug
        meta["task_set"] = new_set
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        meta["status"] = m["status"]
        meta["dir"] = str(d)
        return meta


def delete_episode(ep_id: str) -> dict:
    """彻底删除(仅回收站中的 episode;已保存的请先标废)。"""
    with _LOCK:
        m = find_episode(ep_id)
        if not m:
            raise FileNotFoundError(ep_id)
        if m["status"] != "trash":
            raise ValueError("仅回收站中的 episode 可彻底删除")
        shutil.rmtree(m["dir"])
        return {"id": ep_id, "deleted": True}


def empty_trash() -> int:
    n = len(_scan(TRASH, "trash"))
    if TRASH.is_dir():
        shutil.rmtree(TRASH)
    TRASH.mkdir(parents=True, exist_ok=True)
    return n


def migrate_library_layout() -> int:
    """启动时把旧三层目录 <root>/<slug>/<session>/<id> 迁移为四层 <root>/<set>/<slug>/<session>/<id>。
    set 归属:slug 在当前任务集合中唯一匹配则归该集合,匹配不到或有歧义则归「默认」。
    同时向 meta.json 写入 task_set。返回迁移的 episode 数。"""
    moved = 0
    grouped = load_tasks_grouped()
    for root in (LIBRARY, TRASH):
        if not root.is_dir():
            continue
        for meta_file in sorted(root.glob("*/*/*/meta.json")):
            try:
                m = json.loads(meta_file.read_text())
                slug = m["task_slug"]
                owners = [name for name, ts in grouped.items() if any(t["slug"] == slug for t in ts)]
                task_set = owners[0] if len(owners) == 1 else DEFAULT_SET
                ep_dir = meta_file.parent
                dst = root / task_set / m["task_slug"] / m["session"] / m["id"]
                if dst.exists():
                    log.warning("迁移跳过,目标已存在:%s -> %s", ep_dir, dst)
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                m["task_set"] = task_set
                meta_file.write_text(json.dumps(m, ensure_ascii=False, indent=1))
                shutil.move(str(ep_dir), str(dst))
                moved += 1
            except Exception:  # noqa: BLE001
                log.exception("迁移失败:%s", meta_file)
    if moved:
        log.info("library 目录迁移完成:共 %d 个 episode 迁入 <set>/<slug>/ 结构", moved)
    return moved


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
        key = f"{e.get('task_set', DEFAULT_SET)}/{e['task_slug']}"
        per_task[key] = per_task.get(key, 0) + 1
    return {
        "saved": len(saved),
        "trash": len(eps) - len(saved),
        "minutes": round(sum(e.get("dur", 0) for e in saved) / 60, 1),
        "per_task": per_task,
    }
