"""FastAPI 服务:REST 控制 + 可取消的相机单帧预览 + 静态前端。"""
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config_store, exporter, library
from .arms import ArmManager
from .cams import CamManager
from .paths import ASSETS, STATIC
from .recorder import RecordService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="SO101 Collect Studio")
arms = ArmManager()
cams = CamManager()
rec = RecordService(arms, cams)


@app.on_event("startup")
def _startup():
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


@app.post("/api/rec/start")
def rec_start(req: RecStartReq):
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


@app.post("/api/tasks")
def add_task(req: TaskReq):
    try:
        return library.add_task(req.prompt, req.slug)
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


@app.post("/api/trash/empty")
def trash_empty():
    return {"removed": library.empty_trash()}


# ============ 导出 ============
@app.get("/api/export/sessions")
def export_sessions():
    return exporter.sessions_summary()


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


# ============ 前端 ============
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/assets/{name}")
def asset(name: str):
    p = (ASSETS / name).resolve()
    if p.parent != ASSETS.resolve() or not p.is_file():
        raise HTTPException(404, name)
    return FileResponse(p)
