"""井字棋 300 条采集实例:冻结清单解析、任务映射与完成进度。"""
import hashlib
import json
import re
from functools import lru_cache

from .paths import TIC_TAC_TOE_MANIFEST

TASK_SET = "tic_tac_toe"
SELECTION = "production_300"
SNAPSHOT_SCHEMA = "tic_tac_toe_split_v1"
MANIFEST_VERSION = "v1_seed42"
SOURCE_MANIFEST_SHA256 = "a493f8acd754af0d7efd8c76727033a6d94981abab811f596ee1ca1dc4caa8ed"
LEGACY_TASK_SLUGS = {"black": "black_cube", "white": "white_cube"}
CELL_NAMES = (
    "far_left", "far_center", "far_right",
    "middle_left", "center", "middle_right",
    "near_left", "near_center", "near_right",
)
CELL_LABELS = (
    "远左", "远中", "远右",
    "中左", "中心", "中右",
    "近左", "近中", "近右",
)
CELL_PROMPT_NAMES = (
    "far left", "far center", "far right",
    "middle left", "center", "middle right",
    "near left", "near center", "near right",
)
TACTIC_LABELS = {"win_now": "立即获胜", "block": "阻挡威胁", "strategic": "策略落子"}
_STATE_RE = re.compile(r"^(black|white)_n(\d)_([bew]{9})$")


@lru_cache(maxsize=1)
def load_instances() -> tuple[dict, ...]:
    """加载并严格校验冻结清单,返回带棋盘与任务映射的 300 条实例。"""
    raw = TIC_TAC_TOE_MANIFEST.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_MANIFEST_SHA256:
        raise ValueError("井字棋冻结 manifest 哈希不匹配")
    data = json.loads(raw)
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("井字棋清单 schema 不匹配")
    if data.get("version") != MANIFEST_VERSION:
        raise ValueError("井字棋清单 version 不匹配")
    rows = data.get("collection", {}).get(SELECTION)
    if not isinstance(rows, list) or len(rows) != 300:
        raise ValueError("井字棋清单必须恰好包含 300 条实例")

    instances = []
    seen = set()
    side_counts = {"black": 0, "white": 0}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"井字棋清单第 {index + 1} 条不是对象")
        state_id = str(row.get("state_id", ""))
        match = _STATE_RE.fullmatch(state_id)
        if not match:
            raise ValueError(f"井字棋 state_id 非法:{state_id}")
        side, count_text, board_code = match.groups()
        if state_id in seen:
            raise ValueError(f"井字棋 state_id 重复:{state_id}")
        seen.add(state_id)
        if row.get("side_to_move") != side:
            raise ValueError(f"井字棋执棋方与 state_id 不一致:{state_id}")
        occupied = sum(cell != "e" for cell in board_code)
        if occupied != int(count_text) or occupied != int(row.get("occupied_count", -1)):
            raise ValueError(f"井字棋棋子数量不一致:{state_id}")
        optimal_cell = int(row.get("optimal_cell", -1))
        if not 0 <= optimal_cell < 9 or board_code[optimal_cell] != "e":
            raise ValueError(f"井字棋目标格不是空格:{state_id}")
        if int(row.get("layout_seed", -1)) != 100_000 + index:
            raise ValueError(f"井字棋 layout_seed 顺序错误:{state_id}")
        board = [{"b": "black", "w": "white", "e": None}[cell] for cell in board_code]
        instances.append({
            **row,
            "selection_index": index,
            "display_number": index + 1,
            "board_code": board_code,
            "board": board,
            "task_slug": task_slug(side, optimal_cell),
            "optimal_cell_name": CELL_NAMES[optimal_cell],
            "optimal_cell_label": CELL_LABELS[optimal_cell],
            "tactic_label": TACTIC_LABELS.get(row.get("tactic"), str(row.get("tactic", ""))),
        })
        side_counts[side] += 1
    if side_counts != {"black": 156, "white": 144}:
        raise ValueError(f"井字棋黑白实例数量错误:{side_counts}")
    return tuple(instances)


def instance(selection_index: int) -> dict:
    instances = load_instances()
    if not 0 <= selection_index < len(instances):
        raise IndexError(f"井字棋编号必须在 1-{len(instances)} 之间")
    return instances[selection_index]


def task_slug(side: str, cell: int) -> str:
    """返回颜色与物理目标格唯一对应的任务 slug。"""
    return f"{side}_cube_{CELL_NAMES[cell]}"


def resolve_task(row: dict, tasks: list[dict]) -> dict:
    """按当前执棋方与目标格解析对应的真实语言任务。"""
    slug = row["task_slug"]
    task = next((item for item in tasks if item.get("slug") == slug), None)
    if task is None:
        raise ValueError(f"任务集合 {TASK_SET} 缺少任务 {slug}")
    return task


def episode_metadata(row: dict) -> dict:
    return {
        "manifest_sha256": SOURCE_MANIFEST_SHA256,
        "selection": SELECTION,
        "selection_index": row["selection_index"],
        "state_id": row["state_id"],
        "layout_seed": row["layout_seed"],
        "side_to_move": row["side_to_move"],
        "tactic": row["tactic"],
        "optimal_cell": row["optimal_cell"],
    }


def progress(episodes: list[dict]) -> dict:
    """只按已入库且身份与冻结清单一致的 episode 统计 300 条进度。"""
    instances = load_instances()
    counts: dict[int, int] = {}
    for episode in episodes:
        if episode.get("status") != "saved" or episode.get("task_set") != TASK_SET:
            continue
        meta = episode.get("tic_tac_toe")
        if not isinstance(meta, dict):
            continue
        try:
            index = int(meta["selection_index"])
            row = instances[index]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        valid_task_slugs = {row["task_slug"], LEGACY_TASK_SLUGS[row["side_to_move"]]}
        if (
            meta.get("manifest_sha256") != SOURCE_MANIFEST_SHA256
            or meta.get("selection") != SELECTION
            or meta.get("state_id") != row["state_id"]
            or episode.get("task_slug") not in valid_task_slugs
            or meta.get("side_to_move") != row["side_to_move"]
            or meta.get("layout_seed") != row["layout_seed"]
            or meta.get("tactic") != row["tactic"]
            or meta.get("optimal_cell") != row["optimal_cell"]
        ):
            continue
        counts[index] = counts.get(index, 0) + 1
    completed = sorted(counts)
    duplicates = sorted(index for index, count in counts.items() if count > 1)
    completed_by_side = {
        side: sum(instances[index]["side_to_move"] == side for index in completed)
        for side in LEGACY_TASK_SLUGS
    }
    return {
        "total": len(instances),
        "completed": len(completed),
        "remaining": len(instances) - len(completed),
        "completed_indices": completed,
        "duplicate_indices": duplicates,
        "by_side": {
            "black": {"completed": completed_by_side["black"], "total": 156},
            "white": {"completed": completed_by_side["white"], "total": 144},
        },
    }


def next_incomplete(current_index: int, completed_indices: list[int]) -> int | None:
    """从当前项之后循环查找第一条未完成实例。"""
    total = len(load_instances())
    completed = set(completed_indices)
    for offset in range(1, total + 1):
        candidate = (current_index + offset) % total
        if candidate not in completed:
            return candidate
    return None
