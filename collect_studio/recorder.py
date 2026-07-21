"""录制服务:30Hz 遥操作控制环 + 录制状态机 + staging 落盘。

控制环与录制解耦:teleop 线程常驻(读主动臂→写从动臂→取相机最新帧),
录制状态只决定当前帧是否落盘。保存时后台编码 mp4 并晋升 library,舍弃即删目录。
"""
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2

from . import config_store, library
from .arms import JOINTS, ArmManager
from .cams import ROLES, CamManager
from .paths import LIBRARY, STAGING

log = logging.getLogger("recorder")

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
CAMERA_READY_TIMEOUT = 10.0
CAMERA_RETRY_AFTER = 2.0
CONTROL_READY_TIMEOUT = 3.0


class JpegWriter:
    """异步 JPEG 落盘,不阻塞控制环。"""

    def __init__(self, n_workers=3):
        self.q: queue.Queue = queue.Queue(maxsize=600)
        self.dropped = 0
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(n_workers)]
        for t in self._threads:
            t.start()

    def submit(self, path: str, frame):
        try:
            self.q.put_nowait((path, frame))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while True:
            path, frame = self.q.get()
            try:
                cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            except Exception:  # noqa: BLE001
                log.exception("jpeg write failed")
            finally:
                self.q.task_done()

    def drain(self):
        self.q.join()


class RecordService:
    def __init__(self, arms: ArmManager, cams: CamManager):
        self.arms = arms
        self.cams = cams
        self.session = library.new_session_id()
        self.cur_task: dict | None = None

        self.teleop_on = False
        self.teleop_starting = False
        self.teleop_phase: str | None = None
        self._cancel_start = False
        self.state = "idle"  # idle | rec | paused
        self.ep_dir: Path | None = None
        self.ep_id: str | None = None
        self.rows: list[dict] = []
        self.rec_elapsed = 0.0  # 已录制秒数(不含暂停)
        self.loop_hz = 0.0
        self.overruns = 0
        self.data_f = None

        self.jpeg = JpegWriter()
        self.encode_q: list[dict] = []  # 保存后待编码队列(展示用)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._loop_ready = threading.Event()
        self._loop_error: str | None = None
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ---------- teleop ----------
    def start_teleop(self):
        """异步启动:立即返回,前端轮询 status 里的 starting/phase 显示进度。"""
        with self._lock:
            if self.teleop_on or self.teleop_starting:
                return
            if not (self.arms.follower and self.arms.leader):
                raise RuntimeError("请先在「设备与校准」页连接机械臂")
            self.teleop_starting = True
            self._cancel_start = False
            self.teleop_phase = "准备…"
            self.last_error = None
        threading.Thread(target=self._start_teleop_worker, daemon=True).start()

    def _start_teleop_worker(self):
        torque_enabled = False
        try:
            t0 = time.perf_counter()
            self.teleop_phase = "打开相机流…"
            self.cams.start_bound()
            deadline = time.monotonic() + CAMERA_READY_TIMEOUT
            retry_at = time.monotonic() + CAMERA_RETRY_AFTER
            retried = False
            while time.monotonic() < deadline and not self._cancel_start:
                health = self.cams.bound_health()
                self.teleop_phase = f"等待相机就绪 {health['ready']}/{health['total']}…"
                if health["ready"] == health["total"]:
                    break
                if not retried and time.monotonic() >= retry_at:
                    self.teleop_phase = "重试未就绪相机…"
                    self.cams.retry_failed_bound()
                    retried = True
                time.sleep(0.2)
            else:
                if not self._cancel_start:
                    problems = self.cams.bound_health()["problems"]
                    detail = ", ".join(f"{role}({reason})" for role, reason in problems.items())
                    raise RuntimeError(f"相机未就绪:{detail}")
            t1 = time.perf_counter()
            if self._cancel_start:
                return
            self.teleop_phase = "从动臂上力矩…"
            self.arms.enable_torque()
            torque_enabled = True
            t2 = time.perf_counter()
            if self._cancel_start:
                self.arms.estop()
                return
            self.teleop_phase = "启动 30Hz 控制环…"
            self._stop.clear()
            self._loop_ready.clear()
            self._loop_error = None
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            if not self._loop_ready.wait(CONTROL_READY_TIMEOUT):
                raise RuntimeError("30Hz 控制环未在 3 秒内就绪")
            if self._loop_error:
                raise RuntimeError(self._loop_error)
            if self._cancel_start:
                self.arms.estop()
                return
            self.teleop_on = True
            log.info("teleop started: cams %.2fs, torque %.2fs", t1 - t0, t2 - t1)
        except Exception as e:  # noqa: BLE001
            log.exception("start_teleop failed")
            self._stop.set()
            if torque_enabled:
                self.arms.estop()
            self.last_error = f"遥操作启动失败:{e}"
        finally:
            self.teleop_starting = False
            self.teleop_phase = None

    def stop_teleop(self):
        self._cancel_start = True
        with self._lock:
            if self.state != "idle":
                self.discard()
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self.teleop_on = False
        # 力矩保持,防止从动臂跌落;需要释放用急停/释放按钮

    def _loop(self):
        fps = config_store.load()["record"]["fps"]
        period = 1.0 / fps
        next_t = time.perf_counter()
        last_stat = time.perf_counter()
        n = 0
        while not self._stop.is_set():
            try:
                action = self.arms.leader.get_action()
                self.arms.follower.send_action(action)
                obs = self.arms.follower.bus.sync_read("Present_Position")
                self._loop_ready.set()
                if self.state == "rec":
                    self._capture_frame(obs, action, period)
            except Exception as e:  # noqa: BLE001
                log.exception("teleop loop error")
                self._loop_error = f"控制环异常:{e}"
                self.last_error = self._loop_error
                self._loop_ready.set()
                self.arms.estop()
                self.teleop_on = False
                if self.state != "idle":
                    self._pause_only()
                return
            n += 1
            now = time.perf_counter()
            if now - last_stat >= 1.0:
                self.loop_hz = n / (now - last_stat)
                n, last_stat = 0, now
            next_t += period
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                self.overruns += 1
                next_t = time.perf_counter()

    def _capture_frame(self, obs: dict, action: dict, period: float):
        idx = len(self.rows)
        row = {
            "t": round(self.rec_elapsed, 6),
            "obs": [float(obs[j]) for j in JOINTS],
            "act": [float(action[f"{j}.pos"]) for j in JOINTS],
        }
        self.rows.append(row)
        self.data_f.write(json.dumps(row) + "\n")
        if idx % 30 == 0:
            self.data_f.flush()
        for role in ROLES:
            s = self.cams.stream_for_role(role)
            if s is not None:
                f = s.latest()
                if f is not None:
                    self.jpeg.submit(str(self.ep_dir / "frames" / role / f"{idx:06d}.jpg"), f)
        self.rec_elapsed += period

    # ---------- 录制状态机 ----------
    def rec_start(self, task: dict):
        with self._lock:
            if self.teleop_starting:
                raise RuntimeError("遥操作正在启动中,请稍候几秒")
            if not self.teleop_on:
                raise RuntimeError("请先开始遥操作(从动臂会跟随主动臂)")
            if self.state == "rec":
                return
            if self.state == "paused":  # resume
                self.state = "rec"
                return
            self.cur_task = task
            self.ep_id = library.next_episode_id()
            self.ep_dir = STAGING / self.session / self.ep_id
            for role in ROLES:
                (self.ep_dir / "frames" / role).mkdir(parents=True, exist_ok=True)
            self.rows = []
            self.rec_elapsed = 0.0
            self.data_f = open(self.ep_dir / "data.jsonl", "w")  # noqa: SIM115
            meta = self._meta_dict()
            (self.ep_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            self.state = "rec"

    def rec_pause(self):
        with self._lock:
            if self.state == "rec":
                self.state = "paused"

    def _pause_only(self):
        self.state = "paused" if self.rows else "idle"

    def _meta_dict(self):
        rec = config_store.load()["record"]
        return {
            "id": self.ep_id,
            "session": self.session,
            "task_slug": self.cur_task["slug"],
            "task_set": self.cur_task.get("set", "默认"),
            "task_prompt": self.cur_task["prompt"],
            "fps": rec["fps"],
            "width": rec["width"],
            "height": rec["height"],
            "frames": len(self.rows),
            "dur": round(self.rec_elapsed, 3),
            "joints": JOINTS,
            "cameras": ROLES,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def save(self) -> dict:
        with self._lock:
            if self.state not in ("rec", "paused"):
                raise RuntimeError("当前没有录制中的 episode")
            if len(self.rows) < 5:
                raise RuntimeError("太短了(<5 帧),不予保存")
            self.state = "idle"
            ep_dir, ep_id, meta = self.ep_dir, self.ep_id, self._meta_dict()
            self.data_f.close()
            (ep_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            self.ep_dir, self.ep_id, self.rows, self.data_f = None, None, [], None
            self.rec_elapsed = 0.0
        job = {"id": ep_id, "state": "encoding"}
        self.encode_q.append(job)
        threading.Thread(target=self._finalize, args=(ep_dir, meta, job), daemon=True).start()
        return meta

    def _finalize(self, ep_dir: Path, meta: dict, job: dict):
        try:
            self.jpeg.drain()
            fps = meta["fps"]
            for role in ROLES:
                fdir = ep_dir / "frames" / role
                n = len(list(fdir.glob("*.jpg")))
                if n == 0:
                    raise RuntimeError(f"{role} 没有任何帧")
                out = ep_dir / f"{role}.mp4"
                cmd = [FFMPEG, "-y", "-loglevel", "error",
                       "-framerate", str(fps), "-start_number", "0", "-i", str(fdir / "%06d.jpg"),
                       "-frames:v", str(meta["frames"]),
                       "-c:v", "h264_videotoolbox", "-b:v", "3M",
                       "-pix_fmt", "yuv420p", str(out)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:  # videotoolbox 失败则回退软编
                    cmd[cmd.index("h264_videotoolbox")] = "libx264"
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        raise RuntimeError(f"ffmpeg 编码失败:{r.stderr[-300:]}")
            self._write_parquet(ep_dir, meta)
            shutil.rmtree(ep_dir / "frames")
            dst = LIBRARY / meta.get("task_set", "默认") / meta["task_slug"] / meta["session"] / meta["id"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(ep_dir), str(dst))
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log.exception("finalize failed")
            job["state"] = "error"
            job["msg"] = str(e)
            self.last_error = f"{meta['id']} 保存失败:{e}"

    @staticmethod
    def _write_parquet(ep_dir: Path, meta: dict):
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [json.loads(x) for x in (ep_dir / "data.jsonl").read_text().splitlines() if x.strip()]
        fps = meta["fps"]
        n = len(rows)
        table = pa.table({
            "observation.state": pa.array([r["obs"] for r in rows], type=pa.list_(pa.float32(), 6)),
            "action": pa.array([r["act"] for r in rows], type=pa.list_(pa.float32(), 6)),
            "timestamp": pa.array(np.arange(n, dtype=np.float32) / fps),
            "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        })
        pq.write_table(table, ep_dir / "data.parquet")

    def discard(self):
        with self._lock:
            if self.state not in ("rec", "paused"):
                raise RuntimeError("当前没有录制中的 episode")
            self.state = "idle"
            if self.data_f:
                self.data_f.close()
            ep_dir = self.ep_dir
            self.ep_dir, self.ep_id, self.rows, self.data_f = None, None, [], None
            self.rec_elapsed = 0.0
        if ep_dir and ep_dir.is_dir():
            shutil.rmtree(ep_dir, ignore_errors=True)

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "session": self.session,
            "teleop_on": self.teleop_on,
            "starting": self.teleop_starting,
            "phase": self.teleop_phase,
            "state": self.state,
            "episode_id": self.ep_id,
            "elapsed": round(self.rec_elapsed, 2),
            "frames": len(self.rows),
            "loop_hz": round(self.loop_hz, 1),
            "overruns": self.overruns,
            "jpeg_backlog": self.jpeg.q.qsize(),
            "jpeg_dropped": self.jpeg.dropped,
            "encoding": [j for j in self.encode_q if j["state"] != "done"][-3:],
            "error": self.last_error,
        }
