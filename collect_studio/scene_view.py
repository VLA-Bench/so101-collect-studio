"""场景展示页后端:直接从任务提示词文本解析资产配置(不依赖 desk01)。

指令语法即 desk01 ``build_instruction`` 的唯一模板:
- 块短语 ``{color} {shape}``:color ∈ red/yellow/blue,shape 文案 ∈
  ``cube`` / ``rectangular block`` / ``L-shaped block``(对应资产 id 前缀 cube/cuboid/l_block);
- 放置子句 ``Place <块短语>( and <块短语>)* into the {size} {color} box``,多个子句用
  ``, and `` 或 `` and `` 连接;
- 末尾可选留桌子句 ``, and leave <块短语>... on the desk``(或全句 ``Leave ... on the desk``)。
注意:空盒(无块指向)故意不写进指令,解析不到是预期行为。

所有失败一律降级(payload 里 scene 为 None),不抛到路由层。
"""
import json
import logging
import re
import time

from .paths import CURRENT_DISPLAY_JSON

log = logging.getLogger("scene_view")

# ----------------------------------------------------------------------------------
# 布局/展示常量(数值来自 desk01 scene.py 与其 web/index.html,现已脱钩硬编码)
# ----------------------------------------------------------------------------------
META = {
    "color_hex": {"red": "#d84a4a", "yellow": "#e6c93c", "blue": "#4a78d8"},
    "block_height": {"cube": 0.03, "cuboid": 0.03, "l_block": 0.06},  # 仅 3D 展示用
    "layout": {
        "BOX_Y_M": 0.35,
        "DESK_HALF_X_M": 0.225,
        "BOX_OUTER_X_M": {"large": 0.300, "medium": 0.200, "small": 0.100},
        "BOX_OUTER_Y_M": {"large": 0.200, "medium": 0.200, "small": 0.100},
        "BOX_CAPACITY": {"large": 4, "medium": 3, "small": 2},
        "BLOCK_EXTENT_XY_M": {"cube": [0.030, 0.030], "cuboid": [0.030, 0.050], "l_block": [0.060, 0.080]},
        "BLOCK_SLOTS_M": [[-0.18, 0.19], [-0.09, 0.19], [0.00, 0.19], [0.09, 0.19], [0.18, 0.19]],
    },
}

# ----------------------------------------------------------------------------------
# 提示词文本解析
# ----------------------------------------------------------------------------------
_SHAPE_WORDS = {"cube": "cube", "rectangular block": "cuboid", "l-shaped block": "l_block"}
# 长词必须排在前面,否则 "rectangular block" 会被 "cube" 之外的短词错误截断;
# 块短语容许可选冠词 "the "(desk01 模板本身不带,但手抄/变体指令常见)
_BLOCK_PHRASE_RE = re.compile(r"(?:the\s+)?(red|yellow|blue)\s+(rectangular block|L-shaped block|cube)", re.IGNORECASE)
_BOX_TAIL_RE = re.compile(r"\s*into\s+the\s+(large|medium|small)\s+(red|yellow|blue)\s+box", re.IGNORECASE)
_LEAVE_SUFFIX_RE = re.compile(r",\s*and\s+leave\s+(.+?)\s+on\s+the\s+desk\.?\s*$", re.IGNORECASE)
_LEAVE_ONLY_RE = re.compile(r"Leave\s+(.+?)\s+on\s+the\s+desk\.?\s*", re.IGNORECASE)


def _split_phrases(text: str) -> list[str]:
    """按 _join_phrases 的三种连接形式("A and B" / "A, B, and C")切回短语列表。"""
    return [p for p in (s.strip() for s in re.split(r",\s*and\s+|,\s+|\s+and\s+", text.strip())) if p]


def _parse_block(phrase: str) -> dict | None:
    m = _BLOCK_PHRASE_RE.fullmatch(phrase)
    if not m:
        return None
    color, shape = m.group(1).lower(), _SHAPE_WORDS[m.group(2).lower()]
    return {"id": f"{shape}_{color}", "shape": shape, "color": color}


def parse_instruction(prompt: str) -> dict | None:
    """从提示词文本解析资产配置 → {"boxes": [...], "blocks": [...](含 target)}。

    boxes 按出现顺序,id=size_color;blocks 的 target = 盒 id 或 None(留桌)。
    解析不出任何块/盒(如中文 prompt)或语法不符 → 返回 None。
    """
    try:
        text = (prompt or "").strip()
        leave_text = None
        m = _LEAVE_SUFFIX_RE.search(text)
        if m:
            leave_text, text = m.group(1), text[: m.start()]
        else:
            m = _LEAVE_ONLY_RE.fullmatch(text)
            if m:
                leave_text, text = m.group(1), ""

        boxes: list[dict] = []
        blocks: dict[str, dict] = {}  # 插入序:放置块在前,留桌块在后

        if text:
            if not text.startswith("Place "):
                return None
            body = text[len("Place "):]
            pos = 0
            for m in _BOX_TAIL_RE.finditer(body):
                seg = re.sub(r"^[\s,]+(?:and\s+)?", "", body[pos : m.start()])
                phrases = _split_phrases(seg)
                if not phrases:
                    return None
                size, color = m.group(1).lower(), m.group(2).lower()
                box_id = f"{size}_{color}"
                if not any(b["id"] == box_id for b in boxes):
                    boxes.append({"id": box_id, "size": size, "color": color})
                for ph in phrases:
                    blk = _parse_block(ph)
                    if blk is None:
                        return None
                    blk["target"] = box_id
                    blocks.setdefault(blk["id"], blk)
                pos = m.end()
            if body[pos:].strip(" ,"):  # 盒尾之后有残留 → 语法不符
                return None

        if leave_text is not None:
            phrases = _split_phrases(leave_text)
            if not phrases:
                return None
            for ph in phrases:
                blk = _parse_block(ph)
                if blk is None:
                    return None
                blk["target"] = None
                blocks.setdefault(blk["id"], blk)

        if not boxes and not blocks:
            return None
        return {
            "boxes": boxes,
            "blocks": list(blocks.values()),
            "targets": {b["id"]: b["target"] for b in blocks.values()},
        }
    except Exception:  # noqa: BLE001
        log.exception("解析提示词失败: %r", prompt)
        return None


def payload(prompt: str) -> dict:
    """按提示词文本解析 → {"instruction", "scene"|None, "meta"}。"""
    return {"instruction": prompt, "scene": parse_instruction(prompt), "meta": META}


# ============ 当前选中任务(采集台上报,JSON 文件为准) ============

def read_current() -> dict | None:
    try:
        if not CURRENT_DISPLAY_JSON.is_file():
            return None
        data = json.loads(CURRENT_DISPLAY_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("prompt"):
            return None
        return data
    except Exception:  # noqa: BLE001
        log.exception("读取 current_display.json 失败")
        return None


def write_current(
    task_set: str | None,
    task_slug: str | None,
    prompt: str,
    selection_index: int | None = None,
) -> dict:
    data = {
        "task_set": task_set,
        "task_slug": task_slug,
        "prompt": prompt,
        "ts": time.time(),
    }
    if selection_index is not None:
        data["selection_index"] = selection_index
    try:
        CURRENT_DISPLAY_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURRENT_DISPLAY_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CURRENT_DISPLAY_JSON)
    except Exception:  # noqa: BLE001
        log.exception("写入 current_display.json 失败")
    return data
