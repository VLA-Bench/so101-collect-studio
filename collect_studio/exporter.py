"""v2.1 数据集导出器:从 library 打包 + EEF 注入 + 自动校验。

产出严格对齐 LeRobot v2.1 目录规范:
  meta/{info.json, episodes.jsonl, tasks.jsonl, episodes_stats.jsonl}
  data/chunk-000/episode_%06d.parquet
  videos/chunk-000/<video_key>/episode_%06d.mp4   (直接拷贝,不重编码)
"""
import json
import logging
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import fk, library
from .paths import EXPORTS

log = logging.getLogger("exporter")

CHUNK = 1000
ROLES = ["wrist", "left_rear", "right_rear"]

JOB = {"state": "idle"}  # idle|running|done|error


def start_export(name: str, selection: list[dict], delta_frame: str = "base"):
    """selection: [{task_slug, session}];空列表 = 全部已保存 episode。"""
    if JOB.get("state") == "running":
        raise RuntimeError("已有导出任务在运行")
    JOB.clear()
    JOB.update({"state": "running", "name": name, "progress": 0, "msg": "收集 episode…"})
    threading.Thread(target=_run, args=(name, selection, delta_frame), daemon=True).start()


def _selected_episodes(selection):
    eps = [e for e in library.list_episodes() if e["status"] == "saved"]
    if selection:
        keys = {(s["task_slug"], s["session"]) for s in selection}
        eps = [e for e in eps if (e["task_slug"], e["session"]) in keys]
    eps.sort(key=lambda e: e["id"])
    return eps


def _stats_entry(arr: np.ndarray) -> dict:
    return {
        "min": arr.min(axis=0).tolist(),
        "max": arr.max(axis=0).tolist(),
        "mean": arr.mean(axis=0).tolist(),
        "std": arr.std(axis=0).tolist(),
        "count": [len(arr)],
    }


def _video_stats(mp4: Path, n_samples=8) -> dict:
    cap = cv2.VideoCapture(str(mp4))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    for i in np.linspace(0, max(total - 1, 0), min(n_samples, max(total, 1))).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            frames.append(f[..., ::-1].astype(np.float32) / 255.0)  # BGR→RGB
    cap.release()
    if not frames:
        z = [[[0.0]], [[0.0]], [[0.0]]]
        return {"min": z, "max": z, "mean": z, "std": z, "count": [0]}
    x = np.stack(frames)  # [n,h,w,3]
    def chan(fn):
        return fn(x, axis=(0, 1, 2)).reshape(3, 1, 1).tolist()
    return {"min": chan(np.min), "max": chan(np.max),
            "mean": chan(np.mean), "std": chan(np.std), "count": [len(frames)]}


def _mp4_frame_count(mp4: Path) -> int:
    cap = cv2.VideoCapture(str(mp4))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return n


def _run(name: str, selection, delta_frame: str):
    try:
        eps = _selected_episodes(selection)
        if not eps:
            raise RuntimeError("选择范围内没有已保存的 episode")
        out = EXPORTS / name
        if out.exists():
            shutil.rmtree(out)
        (out / "meta").mkdir(parents=True)
        (out / "data" / "chunk-000").mkdir(parents=True)
        for r in ROLES:
            (out / "videos" / "chunk-000" / f"observation.images.{r}").mkdir(parents=True)

        # 任务表
        task_prompts = {}
        for e in eps:
            task_prompts.setdefault(e["task_prompt"], len(task_prompts))
        fps = eps[0]["fps"]
        w, h = eps[0]["width"], eps[0]["height"]

        episodes_jsonl, stats_jsonl = [], []
        total_frames = 0
        checks = {"frame_mismatch": [], "eef_residual_max": 0.0, "quat_norm_err": 0.0}
        global_idx = 0

        for ei, e in enumerate(eps):
            JOB.update({"progress": int(ei / len(eps) * 90), "msg": f"打包 {e['id']} ({ei+1}/{len(eps)})"})
            src = Path(e["dir"])
            t = pq.read_table(src / "data.parquet")
            n = t.num_rows
            states = np.array(t["observation.state"].to_pylist(), dtype=np.float32)
            actions = np.array(t["action"].to_pylist(), dtype=np.float32)

            obs_eef, act_eef = fk.eef_series(states)
            # 校验:位置增量回代
            if n > 1:
                resid = np.abs(states_resid := (obs_eef[:-1, :3] + act_eef[:-1, :3] - obs_eef[1:, :3])).max()
                checks["eef_residual_max"] = max(checks["eef_residual_max"], float(resid))
            qn = np.abs(np.linalg.norm(obs_eef[:, 3:], axis=1) - 1).max()
            checks["quat_norm_err"] = max(checks["quat_norm_err"], float(qn))

            task_idx = task_prompts[e["task_prompt"]]
            table = pa.table({
                "observation.state": pa.array(states.tolist(), type=pa.list_(pa.float32(), 6)),
                "action": pa.array(actions.tolist(), type=pa.list_(pa.float32(), 6)),
                "observation.state.eef": pa.array(obs_eef.tolist(), type=pa.list_(pa.float32(), 7)),
                "action.eef": pa.array(act_eef.tolist(), type=pa.list_(pa.float32(), 7)),
                "timestamp": pa.array(np.arange(n, dtype=np.float32) / fps),
                "frame_index": pa.array(np.arange(n, dtype=np.int64)),
                "episode_index": pa.array(np.full(n, ei, dtype=np.int64)),
                "index": pa.array(np.arange(global_idx, global_idx + n, dtype=np.int64)),
                "task_index": pa.array(np.full(n, task_idx, dtype=np.int64)),
            })
            pq.write_table(table, out / "data" / "chunk-000" / f"episode_{ei:06d}.parquet")

            ep_stats = {
                "observation.state": _stats_entry(states),
                "action": _stats_entry(actions),
                "observation.state.eef": _stats_entry(obs_eef),
                "action.eef": _stats_entry(act_eef),
                "timestamp": _stats_entry(np.arange(n, dtype=np.float32).reshape(-1, 1) / fps),
                "frame_index": _stats_entry(np.arange(n, dtype=np.int64).reshape(-1, 1)),
                "episode_index": _stats_entry(np.full((n, 1), ei, dtype=np.int64)),
                "index": _stats_entry(np.arange(global_idx, global_idx + n, dtype=np.int64).reshape(-1, 1)),
                "task_index": _stats_entry(np.full((n, 1), task_idx, dtype=np.int64)),
            }
            for r in ROLES:
                src_mp4 = src / f"{r}.mp4"
                dst_mp4 = out / "videos" / "chunk-000" / f"observation.images.{r}" / f"episode_{ei:06d}.mp4"
                shutil.copy2(src_mp4, dst_mp4)
                vn = _mp4_frame_count(dst_mp4)
                if abs(vn - n) > 1:
                    checks["frame_mismatch"].append({"episode": e["id"], "cam": r, "video": vn, "parquet": n})
                ep_stats[f"observation.images.{r}"] = _video_stats(dst_mp4)

            episodes_jsonl.append({"episode_index": ei, "tasks": [e["task_prompt"]], "length": n})
            stats_jsonl.append({"episode_index": ei, "stats": ep_stats})
            global_idx += n
            total_frames += n

        JOB.update({"progress": 92, "msg": "写 meta…"})
        feat_num = lambda shape, names: {"dtype": "float32", "shape": [shape], "names": names}  # noqa: E731
        motors = [f"{j}.pos" for j in ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]]
        features = {
            "observation.state": feat_num(6, motors),
            "action": feat_num(6, motors),
            "observation.state.eef": feat_num(7, ["x", "y", "z", "qw", "qx", "qy", "qz"]),
            "action.eef": feat_num(7, ["dx", "dy", "dz", "dqw", "dqx", "dqy", "dqz"]),
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        }
        for r in ROLES:
            features[f"observation.images.{r}"] = {
                "dtype": "video", "shape": [h, w, 3], "names": ["height", "width", "channels"],
                "info": {"video.fps": fps, "video.height": h, "video.width": w,
                         "video.channels": 3, "video.codec": "h264", "video.pix_fmt": "yuv420p",
                         "video.is_depth_map": False, "has_audio": False},
            }
        info = {
            "codebase_version": "v2.1",
            "robot_type": "so101",
            "total_episodes": len(eps),
            "total_frames": total_frames,
            "total_tasks": len(task_prompts),
            "total_videos": len(eps) * len(ROLES),
            "total_chunks": 1,
            "chunks_size": CHUNK,
            "fps": fps,
            "splits": {"train": f"0:{len(eps)}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": features,
            # 自定义扩展说明
            "eef": {"frame": delta_frame, "convention": "dq = q_{t+1} * q_t^{-1} (base frame), last frame identity",
                    "fk_target": "gripper_frame_link", "urdf": "so101_new_calib.urdf"},
        }
        (out / "meta" / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        with open(out / "meta" / "episodes.jsonl", "w") as f:
            for x in episodes_jsonl:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        with open(out / "meta" / "tasks.jsonl", "w") as f:
            for prompt, i in task_prompts.items():
                f.write(json.dumps({"task_index": i, "task": prompt}, ensure_ascii=False) + "\n")
        with open(out / "meta" / "episodes_stats.jsonl", "w") as f:
            for x in stats_jsonl:
                f.write(json.dumps(x) + "\n")

        report = {
            "episodes": len(eps), "frames": total_frames, "tasks": len(task_prompts),
            "frame_mismatch": checks["frame_mismatch"],
            "eef_residual_max": checks["eef_residual_max"],
            "quat_norm_err": checks["quat_norm_err"],
            "ok": not checks["frame_mismatch"] and checks["eef_residual_max"] < 1e-4,
            "path": str(out),
        }
        (out / "meta" / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        JOB.update({"state": "done", "progress": 100, "msg": "完成", "report": report})
    except Exception as e:  # noqa: BLE001
        log.exception("export failed")
        JOB.update({"state": "error", "msg": str(e)})


def sessions_summary() -> list[dict]:
    """按 task+session 汇总,供导出页勾选。"""
    agg: dict[tuple, dict] = {}
    for e in library.list_episodes():
        if e["status"] != "saved":
            continue
        k = (e["task_slug"], e["session"])
        a = agg.setdefault(k, {"task_slug": k[0], "session": k[1], "count": 0, "minutes": 0.0})
        a["count"] += 1
        a["minutes"] += e.get("dur", 0) / 60
    out = sorted(agg.values(), key=lambda x: (x["session"], x["task_slug"]), reverse=True)
    for a in out:
        a["minutes"] = round(a["minutes"], 1)
    return out
