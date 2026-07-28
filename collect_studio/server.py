"""FastAPI 服务:REST 控制 + 可取消的相机单帧预览 + 静态前端。"""
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config_store, exporter, library, scene_view, scenes
from .arms import ArmManager
from .cams import CamManager
from .paths import ASSETS, STATIC
from .recorder import RecordService

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="SO101 Collect Studio")
arms = ArmManager()
cams = CamManager()
rec = RecordService(arms, cams)


@app.on_event("startup")
def _startup():
    try:
        library.migrate_library_layout()  # 旧 <slug>/ 三层目录迁入 <set>/<slug>/ 四层结构
    except Exception:  # noqa: BLE001
        log.exception("library layout migration failed")
    try:
        cams.ensure_default_binding()
    except Exception:  # noqa: BLE001
        log.exception("default camera binding failed")


# ============ 总状态 ============
@app.get("/api/status")
def status():
    return {
        "arms": arms.status(),
        "cams": cams.status(),
        "rec": rec.status(),
        "tasks": library.load_tasks(),
        "tasks_by_set": library.load_tasks_grouped(),  # 不去重的分组视图,供前端按集合过滤
        "task_sets": library.list_task_sets(),
        "scene_sets": scenes.scene_ids(),  # 场景派生集合(只读,前端禁用添加/导入)
        "stats": library.stats(),
        "staging_leftovers": library.recover_staging(),
        "ts": time.time(),
    }


# ============ 机械臂 ============
@app.post("/api/arms/wiggle")
def arms_wiggle():
    arms.wiggle_identify()
    return arms.wiggle


@app.post("/api/arms/calib_import")
def calib_import():
    try:
        return arms.import_calibration()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@app.post("/api/arms/health")
def arms_health():
    return arms.health_check()


@app.post("/api/arms/connect")
def arms_connect():
    try:
        return arms.connect()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@app.post("/api/arms/disconnect")
def arms_disconnect():
    rec.stop_teleop()
    arms.disconnect()
    return {"ok": True}


@app.post("/api/arms/estop")
def arms_estop():
    rec.stop_teleop()
    arms.estop()
    return {"ok": True}


# ============ 相机 ============
@app.post("/api/cams/start_all")
def cams_start_all():
    cams.start_all()
    return cams.status()


@app.post("/api/cams/start_bound")
def cams_start_bound():
    cams.start_bound()
    return cams.status()


class StartUidReq(BaseModel):
    unique_id: str


@app.post("/api/cams/start_uid")
def cams_start_uid(req: StartUidReq):
    try:
        return cams.start_uid(req.unique_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


class BindReq(BaseModel):
    role: str
    unique_id: str


@app.post("/api/cams/bind")
def cams_bind(req: BindReq):
    return cams.bind(req.role, req.unique_id)


class UnbindReq(BaseModel):
    role: str


@app.post("/api/cams/unbind")
def cams_unbind(req: UnbindReq):
    return cams.unbind(req.role)


def _frame_response(stream):
    buf = stream.latest_jpeg() if stream else None
    if not buf:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(buf, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/frame/role/{role}.jpg")
def frame_role(role: str):
    return _frame_response(cams.stream_for_role(role))


@app.get("/frame/uid/{uid}.jpg")
def frame_uid(uid: str):
    return _frame_response(cams.stream_for_uid(uid))


# ============ 遥操作 / 录制 ============
@app.post("/api/teleop/start")
def teleop_start():
    try:
        rec.start_teleop()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.post("/api/teleop/stop")
def teleop_stop():
    rec.stop_teleop()
    return {"ok": True}


class RecStartReq(BaseModel):
    task_slug: str
    task_set: str | None = None  # 任务所属集合;空 = 合并列表里第一个匹配 slug 的任务


@app.post("/api/rec/start")
def rec_start(req: RecStartReq):
    if req.task_set:
        try:
            task = next((t for t in library.load_tasks(req.task_set) if t["slug"] == req.task_slug), None)
        except FileNotFoundError:  # 集合文件不存在
            task = None
    else:
        task = next((t for t in library.load_tasks() if t["slug"] == req.task_slug), None)
    if not task:
        raise HTTPException(404, f"任务 {req.task_slug} 不存在")
    try:
        rec.rec_start(task)
        return rec.status()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.post("/api/rec/pause")
def rec_pause():
    rec.rec_pause()
    return rec.status()


@app.post("/api/rec/save")
def rec_save():
    try:
        return rec.save()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.post("/api/rec/discard")
def rec_discard():
    try:
        rec.discard()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


# ============ 任务 ============
class TaskReq(BaseModel):
    prompt: str
    slug: str | None = None
    set: str | None = None  # 目标任务集合;空 = 「默认」集合(tasks.json)


@app.post("/api/tasks")
def add_task(req: TaskReq):
    if scenes.is_scene_set(req.set or library.DEFAULT_SET):
        raise HTTPException(400, f"集合 {req.set or library.DEFAULT_SET} 由场景生成,请在 /scene 制定模式中编辑")
    try:
        return library.add_task(req.prompt, req.slug, req.set)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class TaskImportReq(BaseModel):
    content: str  # tasks.jsonl 文件全文
    set: str | None = None  # 目标任务集合


@app.post("/api/tasks/import")
def import_tasks(req: TaskImportReq):
    if scenes.is_scene_set(req.set or library.DEFAULT_SET):
        raise HTTPException(400, f"集合 {req.set or library.DEFAULT_SET} 由场景生成,请在 /scene 制定模式中编辑")
    try:
        return library.import_tasks(req.content, req.set)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ============ episode 浏览 ============
@app.get("/api/episodes")
def episodes():
    return library.list_episodes()


@app.get("/api/episodes/{ep_id}/video/{role}")
def episode_video(ep_id: str, role: str):
    m = library.find_episode(ep_id)
    if not m:
        raise HTTPException(404, ep_id)
    p = Path(m["dir"]) / f"{role}.mp4"
    if not p.is_file():
        raise HTTPException(404, f"{role}.mp4")
    return FileResponse(p, media_type="video/mp4")


@app.post("/api/episodes/{ep_id}/trash")
def episode_trash(ep_id: str):
    return library.move_episode(ep_id, to_trash=True)


@app.post("/api/episodes/{ep_id}/restore")
def episode_restore(ep_id: str):
    return library.move_episode(ep_id, to_trash=False)


class EpisodeMetaReq(BaseModel):
    task_prompt: str | None = None  # 新提示词文本
    task_slug: str | None = None    # 同时归入已有任务(改 slug 并移动目录)
    task_set: str | None = None     # 目标任务所属集合(与 task_slug 一起定位;空 = 首个匹配 slug 的集合)


@app.post("/api/episodes/{ep_id}/meta")
def episode_meta(ep_id: str, req: EpisodeMetaReq):
    try:
        return library.update_episode(ep_id, req.task_prompt, req.task_slug, req.task_set)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/episodes/{ep_id}/delete")
def episode_delete(ep_id: str):
    """彻底删除(仅回收站)。"""
    try:
        return library.delete_episode(ep_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/trash/empty")
def trash_empty():
    return {"removed": library.empty_trash()}


# ============ 导出 ============
@app.get("/api/export/tasks")
def export_tasks():
    return exporter.tasks_summary()


class ExportReq(BaseModel):
    name: str
    selection: list[dict] = []


@app.post("/api/export")
def export_start(req: ExportReq):
    try:
        exporter.start_export(req.name, req.selection)
        return exporter.JOB
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.get("/api/export/status")
def export_status():
    return exporter.JOB


# ============ 场景制定(scenes.json 权威) ============
@app.get("/api/scenes")
def scenes_get():
    """展开场景(含派生 subtasks/instruction) + 全局统计 + 元数据(供制定模式编辑器)。"""
    payload = scenes.scenes_payload()
    payload["meta"] = {**scene_view.META, **payload["meta"]}  # 展示常量 + 资产目录/布局
    return payload


class ScenesSaveReq(BaseModel):
    scenes: list[dict]
    force: bool = False


@app.post("/api/scenes")
def scenes_save(req: ScenesSaveReq):
    """校验并写回 scenes.json;校验错误 400,重复资产 409(force 放行),成功同步派生 tasks 文件。"""
    try:
        scenes.save_scenes(req.model_dump(), force=req.force)
    except scenes.SceneValidationError as e:
        raise HTTPException(400, detail={"errors": e.errors}) from e
    except scenes.SceneConflictError as e:
        raise HTTPException(409, detail={"conflicts": e.conflicts}) from e
    return scenes_get()


class ClassifyReq(BaseModel):
    boxes: list[str]
    blocks: list[str]
    targets: dict


@app.post("/api/scenes/classify")
def scenes_classify(req: ClassifyReq):
    """对一组资产 + targets 现算子任务与指令(编辑器实时预览)。"""
    try:
        return scenes.classify(req.boxes, req.blocks, req.targets)
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"资产非法: {e}") from e


# ============ 场景展示页(只读) ============
class DisplayCurrentReq(BaseModel):
    task_set: str | None = None
    task_slug: str | None = None
    prompt: str


@app.post("/api/display/current")
def display_current(req: DisplayCurrentReq):
    """采集台前端上报当前选中任务,落盘 current_display.json(重启保留)。"""
    return scene_view.write_current(req.task_set, req.task_slug, req.prompt)


@app.get("/api/display/scene")
def display_scene():
    """展示页轮询:当前任务 + 资产配置。优先按 instruction 精确匹配 scenes.json
    (返回完整场景,含未被点名的干扰空盒与 subtasks);未命中回退提示词文本解析。"""
    current = scene_view.read_current()
    if not current:
        return {"current": None, "scene": None, "instruction": None, "meta": scene_view.META}
    prompt = current["prompt"]
    hit = scenes.instruction_index().get(prompt)
    if hit:
        sc, task = hit["scene"], hit["task"]
        scene = {
            "scene_id": hit["scene_id"],
            "subtasks": task["subtasks"],
            "boxes": sc["boxes"],
            "blocks": [dict(b, target=task["targets"].get(b["id"])) for b in sc["blocks"]],
            "targets": task["targets"],
        }
        return {"current": current, "scene": scene, "instruction": prompt, "meta": scene_view.META}
    scene_payload = scene_view.payload(prompt)
    return {
        "current": current,
        "scene": scene_payload.get("scene"),
        "instruction": prompt,
        "meta": scene_view.META,
    }


# ============ 前端 ============
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/scene")
def scene_page():
    return FileResponse(STATIC / "scene.html")


@app.get("/assets/{name}")
def asset(name: str):
    p = (ASSETS / name).resolve()
    if p.parent != ASSETS.resolve() or not p.is_file():
        raise HTTPException(404, name)
    return FileResponse(p)
