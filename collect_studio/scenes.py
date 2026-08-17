"""场景权威逻辑 + 存储(迁移自 so101-vla-project/tasks/desk01/scene.py,2026-07-26 脱钩)。

- ``~/so101_data/scenes.json`` 是场景与任务的唯一权威数据源(collapse 形式:资产用规范 id
  数组,任务只存 targets;subtasks/instruction 均由本模块派生,不入库)。
- 每个场景单向同步派生一个任务集合文件 ``~/so101_data/tasks_<场景id>.jsonl``(LeRobot 行),
  供采集台现有任务集合机制消费;**不删除**任何旧 tasks 文件(防误删已录数据关联)。
- 迁移来源:desk01 scene.py 纯 Python 部分(资产目录/expand_scene/classify_targets/
  build_instruction/validate_scene/box_centers/布局常量)与 desk01 web/server.py 的
  保存逻辑(校验 → 重复资产 409 force → 原子写)。MuJoCo/enumerate/success 部分未迁移。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

from .paths import DATA_ROOT, SCENES_JSON

log = logging.getLogger("scenes")

# 子任务命名(沿用 desk01 用户定义)。子任务只是对同一份资产给出的不同 targets,由分类器派生。
SUBTASKS = {
    "T1": "夹取所有物块",
    "T2": "颜色对应放入",
    "T3": "颜色不对应放入",
    "T4": "按形状放入",
    "T5": "按形状+颜色放入",
}

SHAPE_WORDS = {"cube": "cube", "cuboid": "rectangular block", "l_block": "L-shaped block"}

# --------------------------------------------------------------------------------------
# 资产目录与规范 id(盒 = size_color、块 = shape_color,全局唯一)
# --------------------------------------------------------------------------------------

ASSET_COLORS = ("red", "yellow", "blue")
ASSET_SIZES = ("large", "medium", "small")
ASSET_SHAPES = ("cube", "cuboid", "l_block")

ALL_BOX_IDS = tuple(f"{size}_{color}" for size in ASSET_SIZES for color in ASSET_COLORS)
ALL_BLOCK_IDS = tuple(f"{shape}_{color}" for shape in ASSET_SHAPES for color in ASSET_COLORS)


def box_attrs(box_id: str) -> dict[str, str]:
    """规范盒 id → {id, size, color}。id 形如 ``large_red``。"""
    size, color = box_id.rsplit("_", 1)  # color 无下划线,size 可能有(无)——rsplit 稳妥
    if size not in ASSET_SIZES or color not in ASSET_COLORS:
        raise ValueError(f"非法盒 id: {box_id!r}")
    return {"id": box_id, "size": size, "color": color}


def block_attrs(block_id: str) -> dict[str, str]:
    """规范块 id → {id, shape, color}。id 形如 ``cube_red`` / ``l_block_yellow``。"""
    shape, color = block_id.rsplit("_", 1)  # l_block 含下划线,故用 rsplit 只切最后一段颜色
    if shape not in ASSET_SHAPES or color not in ASSET_COLORS:
        raise ValueError(f"非法块 id: {block_id!r}")
    return {"id": block_id, "shape": shape, "color": color}


def expand_scene(raw: dict) -> dict[str, Any]:
    """把 scenes.json 里的原始场景(资产为规范 id 数组)展开成内部对象形式。"""
    return {
        "scene_id": raw["id"],
        "boxes": [box_attrs(bid) for bid in raw["boxes"]],
        "blocks": [block_attrs(bid) for bid in raw["blocks"]],
        "tasks": [dict(task) for task in raw["tasks"]],
    }


def collapse_scene(scene: dict) -> dict[str, Any]:
    """内部对象形式 → scenes.json 原始形式(只保留权威字段:资产 id + targets)。"""
    return {
        "id": scene["scene_id"],
        "boxes": [b["id"] for b in scene["boxes"]],
        "blocks": [b["id"] for b in scene["blocks"]],
        "tasks": [{"targets": task["targets"]} for task in scene["tasks"]],
    }


# --------------------------------------------------------------------------------------
# 布局常量(数值来自 desk01 scene.py,与 3D 展示共用)
# --------------------------------------------------------------------------------------

BOX_Y_M = 0.35
DESK_HALF_X_M = 0.225
BOX_OUTER_X_M = {"large": 0.300, "medium": 0.200, "small": 0.100}
BOX_OUTER_Y_M = {"large": 0.200, "medium": 0.200, "small": 0.100}
BOX_CAPACITY = {"large": 4, "medium": 3, "small": 2}  # 小盒=2 为资产事实;中/大为 desk01 的假设值
# 物块 footprint(俯视外形,米),供前端示意图用;z 高度不入库。
BLOCK_EXTENT_XY_M = {"cube": (0.030, 0.030), "cuboid": (0.030, 0.050), "l_block": (0.060, 0.080)}
# 物块出生槽位:前排单排,最多 5 个,x 均匀铺开(y=0.19 是 desk01 实测值,勿随手改)。
BLOCK_SLOT_Y_M = 0.19
BLOCK_SLOTS_M = tuple((x, BLOCK_SLOT_Y_M) for x in (-0.18, -0.09, 0.00, 0.09, 0.18))

# 盒子摆放白名单(按尺寸多重集判定);禁止大中。
BOX_CONFIG_WHITELIST = (
    ("large",),
    ("medium",),
    ("small",),
    ("medium", "medium"),
    ("large", "small"),
    ("medium", "small"),
    ("small", "small"),
    ("medium", "small", "small"),
    ("small", "small", "small"),
)


def box_centers(sizes: list[str]) -> list[float]:
    """把盒子沿桌面后排从左到右等间隙排开,返回中心 x 列表。"""
    widths = [BOX_OUTER_X_M[size] for size in sizes]
    gap = (2 * DESK_HALF_X_M - sum(widths)) / (len(widths) + 1)
    centers: list[float] = []
    cursor = -DESK_HALF_X_M + gap
    for width in widths:
        centers.append(round(cursor + width / 2.0, 4))
        cursor += width + gap
    return centers


# --------------------------------------------------------------------------------------
# 子任务判别(明确成文的谓词);classify_targets 返回全部命中的子任务。
# --------------------------------------------------------------------------------------


def _placed(targets: dict[str, str | None]) -> dict[str, str]:
    return {bid: tid for bid, tid in targets.items() if tid is not None}


def _is_place_all(scene: dict, targets: dict) -> bool:  # T1:全部放入,无留桌
    placed = _placed(targets)
    return len(placed) >= 1 and len(placed) == len(targets)


def _is_color_match(scene: dict, targets: dict) -> bool:  # T2:每个放置块与盒同色
    box_color = {b["id"]: b["color"] for b in scene["boxes"]}
    blk_color = {b["id"]: b["color"] for b in scene["blocks"]}
    placed = _placed(targets)
    return len(placed) >= 1 and all(blk_color[bid] == box_color[tid] for bid, tid in placed.items())


def _is_color_mismatch(scene: dict, targets: dict) -> bool:  # T3:每个放置块与盒异色
    box_color = {b["id"]: b["color"] for b in scene["boxes"]}
    blk_color = {b["id"]: b["color"] for b in scene["blocks"]}
    placed = _placed(targets)
    return len(placed) >= 1 and all(blk_color[bid] != box_color[tid] for bid, tid in placed.items())


def _pure_groups(scene: dict, targets: dict, attr: str) -> list[set[str]]:
    """返回属性值完整进入同一盒、且目标盒不混入其他属性值的物块组。"""
    attrs = {b["id"]: b[attr] for b in scene["blocks"]}
    placed = _placed(targets)
    by_attr: dict[str, list[str]] = {}
    for bid, value in attrs.items():
        by_attr.setdefault(value, []).append(bid)
    groups: list[set[str]] = []
    for value, ids in by_attr.items():
        destinations = {targets[bid] for bid in ids}
        if len(destinations) != 1 or None in destinations:
            continue
        target = next(iter(destinations))
        if all(attrs[bid] == value for bid, tid in placed.items() if tid == target):
            groups.append(set(ids))
    return groups


def _is_by_shape(scene: dict, targets: dict) -> bool:  # T4:同形状物块整体入同一纯形状盒
    placed = _placed(targets)
    shape_groups = _pure_groups(scene, targets, "shape")
    if not shape_groups:
        return False
    covered = set().union(*shape_groups)
    return set(placed) <= covered and any(len(group) >= 2 for group in shape_groups)


def _is_by_shape_color(scene: dict, targets: dict) -> bool:  # T5:形状组与颜色组共同覆盖已放入物块
    placed = _placed(targets)
    shape_groups = _pure_groups(scene, targets, "shape")
    color_groups = _pure_groups(scene, targets, "color")
    if not shape_groups or not color_groups:
        return False
    covered = set().union(*shape_groups, *color_groups)
    return set(placed) <= covered and any(len(group) >= 2 for group in (*shape_groups, *color_groups))


SUBTASK_PREDICATES: dict[str, Callable[[dict, dict], bool]] = {
    "T1": _is_place_all,
    "T2": _is_color_match,
    "T3": _is_color_mismatch,
    "T4": _is_by_shape,
    "T5": _is_by_shape_color,
}


def classify_targets(scene: dict, targets: dict[str, str | None]) -> list[str]:
    """返回该 targets 命中的全部子任务(空列表 = 未归类 / 基础放置)。scene 需为展开形式。"""
    subtasks = [sid for sid, pred in SUBTASK_PREDICATES.items() if pred(scene, targets)]
    if "T5" in subtasks and "T4" in subtasks:
        subtasks.remove("T4")  # T5 为形状+颜色组合任务,与纯形状 T4 互斥
    return subtasks


# --------------------------------------------------------------------------------------
# 指令生成(唯一显式指令:点名每块进哪个『尺寸+颜色』盒,留桌块单列)
# --------------------------------------------------------------------------------------


def _block_phrase(block: dict) -> str:
    return f"{block['color']} {SHAPE_WORDS[block['shape']]}"


def _join_phrases(phrases: list[str]) -> str:
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return ", ".join(phrases[:-1]) + f", and {phrases[-1]}"


def build_instruction(scene: dict, targets: dict[str, str | None]) -> str:
    """由 targets 生成唯一显式指令:点名每块进哪个『尺寸+颜色』盒,留桌块单列。

    空盒(无块指向)故意不写进指令,作为纯视觉干扰项。scene 需为展开形式。
    """
    boxes = scene["boxes"]
    blocks = scene["blocks"]
    clauses: list[str] = []
    for box in boxes:
        members = [b for b in blocks if targets.get(b["id"]) == box["id"]]
        if not members:
            continue
        subject = _join_phrases([_block_phrase(b) for b in members])
        clauses.append(f"{subject} into the {box['size']} {box['color']} box")

    if not clauses:
        body = ""
    # 子句内部已用 "and" 连接多物块时,子句之间改用逗号,避免连续 and 无法断句。
    elif len(clauses) == 2 and any(" and " in clause for clause in clauses):
        body = f"{clauses[0]}, and {clauses[1]}"
    else:
        body = _join_phrases(clauses)
    text = f"Place {body}" if body else ""

    leftovers = [b for b in blocks if targets.get(b["id"]) is None]
    if leftovers:
        phrase = _join_phrases([_block_phrase(b) for b in leftovers])
        text += f", and leave {phrase} on the desk" if text else f"Leave {phrase} on the desk"
    return text


# --------------------------------------------------------------------------------------
# 场景校验(结构层面,轻量);scene 为展开形式
# --------------------------------------------------------------------------------------


def validate_scene(scene: dict) -> list[str]:
    """检查资产合法/唯一、盒摆放白名单、targets 合法性、盒容量。返回错误列表(空=通过)。"""
    errors: list[str] = []
    sid = scene.get("scene_id", "?")
    box_by_id = {b["id"]: b for b in scene["boxes"]}
    blk_by_id = {b["id"]: b for b in scene["blocks"]}
    if len(box_by_id) != len(scene["boxes"]):
        errors.append(f"{sid}: 盒 id 重复")
    if len(blk_by_id) != len(scene["blocks"]):
        errors.append(f"{sid}: 块 id 重复")
    for bid in box_by_id:
        if bid not in ALL_BOX_IDS:
            errors.append(f"{sid}: 未知盒 id {bid!r}")
    for bid in blk_by_id:
        if bid not in ALL_BLOCK_IDS:
            errors.append(f"{sid}: 未知块 id {bid!r}")
    if len(scene["blocks"]) > len(BLOCK_SLOTS_M):
        errors.append(f"{sid}: 物块数 {len(scene['blocks'])} 超过槽位数 {len(BLOCK_SLOTS_M)}")

    sizes = tuple(sorted(b["size"] for b in scene["boxes"]))
    if scene["boxes"] and sizes not in {tuple(sorted(c)) for c in BOX_CONFIG_WHITELIST}:
        errors.append(f"{sid}: 盒摆放 {sizes} 不在白名单内")

    for index, task in enumerate(scene["tasks"]):
        tag = f"{sid}/task{index}"
        targets = task["targets"]
        if set(targets) != set(blk_by_id):
            errors.append(f"{tag}: targets 未覆盖全部物块或含未知块 id")
            continue
        counts: dict[str, int] = {}
        for bid, tid in targets.items():
            if tid is None:
                continue
            if tid not in box_by_id:
                errors.append(f"{tag}: 目标盒 {tid!r} 不存在")
                continue
            counts[tid] = counts.get(tid, 0) + 1
        for tid, n in counts.items():
            cap = BOX_CAPACITY[box_by_id[tid]["size"]]
            if n > cap:
                errors.append(f"{tag}: 盒 {tid} 放入 {n} 块超过容量 {cap}")
    return errors


# --------------------------------------------------------------------------------------
# 存储层:~/so101_data/scenes.json 权威;派生 tasks_<场景id>.jsonl(单向同步)
# --------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_cache: dict = {"mtime": None, "scenes": None}


class SceneValidationError(ValueError):
    """校验失败,errors 为中文错误列表(对应 HTTP 400)。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class SceneConflictError(Exception):
    """重复资产冲突(对应 HTTP 409),conflicts = [{"id", "matches"}]。"""

    def __init__(self, conflicts: list[dict]):
        super().__init__(str(conflicts))
        self.conflicts = conflicts


def load_scenes() -> list[dict]:
    """读 scenes.json(mtime 缓存),展开资产并现算每个 task 的 subtasks/instruction。
    文件不存在 → 空列表;解析失败记日志返回空列表(不抛到路由层)。"""
    mtime = SCENES_JSON.stat().st_mtime if SCENES_JSON.is_file() else None
    with _LOCK:
        if _cache["scenes"] is not None and _cache["mtime"] == mtime:
            return _cache["scenes"]
    if mtime is None:
        scenes: list[dict] = []
    else:
        try:
            raw = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
            scenes = [expand_scene(item) for item in raw.get("scenes", [])]
            for scene in scenes:
                for task in scene["tasks"]:
                    task["subtasks"] = classify_targets(scene, task["targets"])
                    task["instruction"] = build_instruction(scene, task["targets"])
        except Exception:  # noqa: BLE001
            log.exception("读取 scenes.json 失败: %s", SCENES_JSON)
            return []
    with _LOCK:
        _cache.update(mtime=mtime, scenes=scenes)
    return scenes


def scene_ids() -> list[str]:
    """当前场景 id 列表(场景派生任务集合同名,/api/status 的 scene_sets 用)。"""
    return [sc["scene_id"] for sc in load_scenes()]


def save_scenes(data: dict, force: bool = False) -> None:
    """校验并写回 scenes.json(原子写),成功后单向同步派生 tasks 文件。

    逻辑移植自 desk01 web/server.py::save:校验失败 → SceneValidationError(400 语义);
    (盒,块) 多重集相同的重复资产 → SceneConflictError(409 语义),force 才放行。
    """
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list):
        raise SceneValidationError(["scenes 必须是数组"])

    errors: list[str] = []
    ids = [str(raw.get("id")) for raw in raw_scenes]
    if len(set(ids)) != len(ids):
        errors.append("场景 id 重复")

    expanded: list[dict] = []
    for raw in raw_scenes:
        try:
            expanded.append(expand_scene(raw))
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(f"{raw.get('id', '?')}: {exc}")
    if not errors:
        for sc in expanded:
            errors.extend(validate_scene(sc))
    if errors:
        raise SceneValidationError(errors)

    if not force:
        seen: dict[tuple, str] = {}
        conflicts: list[dict] = []
        for raw in raw_scenes:
            key = (tuple(sorted(raw["boxes"])), tuple(sorted(raw["blocks"])))
            if key in seen:
                conflicts.append({"id": raw["id"], "matches": seen[key]})
            else:
                seen[key] = raw["id"]
        if conflicts:
            raise SceneConflictError(conflicts)

    payload = {"scenes": [collapse_scene(sc) for sc in expanded]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    SCENES_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCENES_JSON.with_suffix(SCENES_JSON.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(SCENES_JSON)
    sync_task_files(expanded)


def sync_task_files(scenes: list[dict] | None = None) -> list[str]:
    """对每个场景生成 ~/so101_data/tasks_<场景id>.jsonl(LeRobot 行 {"task_index","task"},
    instruction 由 build_instruction 派生);内容不变不写(避免 mtime 抖动);
    **不删除**任何旧 tasks 文件(防误删已录数据的关联,废弃集合由用户手动清理)。
    返回本次写入的文件名列表。"""
    if scenes is None:
        scenes = load_scenes()
    written: list[str] = []
    for sc in scenes:
        lines = "".join(
            json.dumps({"task_index": i, "task": build_instruction(sc, t["targets"])}, ensure_ascii=False) + "\n"
            for i, t in enumerate(sc["tasks"])
        )
        path = DATA_ROOT / f"tasks_{sc['scene_id']}.jsonl"
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == lines:
                continue
            path.write_text(lines, encoding="utf-8")
            written.append(path.name)
        except Exception:  # noqa: BLE001
            log.exception("同步任务集合文件失败: %s", path)
    return written


def instruction_index() -> dict[str, dict]:
    """instruction → {scene_id, scene(展开), task(含 targets/subtasks)} 索引,
    供展示页精确匹配(含未被指令点名的干扰空盒)。指令撞车保留先出现的。"""
    index: dict[str, dict] = {}
    for sc in load_scenes():
        for task in sc["tasks"]:
            index.setdefault(task["instruction"], {"scene_id": sc["scene_id"], "scene": sc, "task": task})
    return index


def meta() -> dict:
    """编辑器/展示页渲染所需目录与布局常量(对齐 desk01 web/server.py::layout_meta,
    加上 scene_view.META 的颜色/展示常量——在 server 层合并,这里只给场景侧部分)。"""
    return {
        "subtasks": SUBTASKS,
        "box_ids": list(ALL_BOX_IDS),
        "block_ids": list(ALL_BLOCK_IDS),
        "box_attrs": {bid: box_attrs(bid) for bid in ALL_BOX_IDS},
        "block_attrs": {bid: block_attrs(bid) for bid in ALL_BLOCK_IDS},
        "layout": {
            "BOX_Y_M": BOX_Y_M,
            "DESK_HALF_X_M": DESK_HALF_X_M,
            "BOX_OUTER_X_M": BOX_OUTER_X_M,
            "BOX_OUTER_Y_M": BOX_OUTER_Y_M,
            "BOX_CAPACITY": BOX_CAPACITY,
            "BLOCK_EXTENT_XY_M": {k: list(v) for k, v in BLOCK_EXTENT_XY_M.items()},
            "BLOCK_SLOTS_M": [list(s) for s in BLOCK_SLOTS_M],
            "BOX_CONFIG_WHITELIST": [list(c) for c in BOX_CONFIG_WHITELIST],
        },
    }


def scenes_payload() -> dict:
    """GET /api/scenes 的响应:展开场景(含派生 subtasks/instruction) + 全局统计 + 元数据。"""
    scenes = load_scenes()
    per_subtask = {sid: 0 for sid in SUBTASKS}
    unclassified = 0
    for sc in scenes:
        for task in sc["tasks"]:
            if not task["subtasks"]:
                unclassified += 1
            for sid in task["subtasks"]:
                per_subtask[sid] = per_subtask.get(sid, 0) + 1
    return {
        "scenes": scenes,
        "stats": {
            "per_scene_tasks": {sc["scene_id"]: len(sc["tasks"]) for sc in scenes},
            "per_subtask": per_subtask,
            "unclassified": unclassified,
        },
        "meta": meta(),
    }


def classify(boxes: list[str], blocks: list[str], targets: dict) -> dict:
    """对一组资产 + targets 现算子任务与指令(编辑器编辑中实时预览用)。
    资产非法抛 ValueError。"""
    sc = expand_scene({"id": "?", "boxes": boxes, "blocks": blocks, "tasks": []})
    return {"subtasks": classify_targets(sc, targets), "instruction": build_instruction(sc, targets)}


# 集合名合法性沿用 library 的约束(防路径穿越),这里只做入口预检
_SET_NAME_RE = re.compile(r"[\w-]+")


def is_scene_set(name: str | None) -> bool:
    """集合名是否与某场景 id 相同(相同则该集合由场景派生,禁止手动添加/导入任务)。"""
    return bool(name) and bool(_SET_NAME_RE.fullmatch(name)) and name in scene_ids()
