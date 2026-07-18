"""SO101 正运动学:解析 so101_new_calib.urdf,base → gripper_frame_link。

输入是数据集里的归一化关节值(RANGE_M100_100 / gripper RANGE_0_100),
经校准 json 反算 ticks → 角度(2048 ticks = URDF 零位,"new_calib" 即此约定)→ FK。
纯 numpy,无 placo 依赖。
"""
import json
import xml.etree.ElementTree as ET
from functools import lru_cache

import numpy as np

from .paths import LEROBOT_CALIB, URDF

FK_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
TARGET_LINK = "gripper_frame_link"
BASE_LINK = "base"


def _rpy_to_R(r, p, y):
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def _T(xyz, rpy):
    T = np.eye(4)
    T[:3, :3] = _rpy_to_R(*rpy)
    T[:3, 3] = xyz
    return T


def _axis_rot(axis, q):
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    c, s, C = np.cos(q), np.sin(q), 1 - np.cos(q)
    R = np.array([
        [x * x * C + c, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, y * y * C + c, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, z * z * C + c],
    ])
    T = np.eye(4)
    T[:3, :3] = R
    return T


@lru_cache(maxsize=1)
def _chain():
    """解析 URDF,返回 base→gripper_frame_link 的关节序列。"""
    root = ET.parse(URDF).getroot()
    joints = {}
    for j in root.findall("joint"):
        o = j.find("origin")
        a = j.find("axis")
        joints[j.find("child").get("link")] = {
            "name": j.get("name"),
            "type": j.get("type"),
            "parent": j.find("parent").get("link"),
            "xyz": [float(v) for v in (o.get("xyz") if o is not None else "0 0 0").split()],
            "rpy": [float(v) for v in (o.get("rpy") if o is not None else "0 0 0").split()],
            "axis": [float(v) for v in (a.get("axis") if a is not None and a.get("axis") else
                                        (a.get("xyz") if a is not None else "0 0 1")).split()]
            if a is not None else [0, 0, 1],
        }
    chain = []
    link = TARGET_LINK
    while link in joints:  # 走到没有父关节的链接即为根(base)
        j = joints[link]
        chain.append(j)
        link = j["parent"]
    if not chain:
        raise RuntimeError(f"URDF 中找不到 {TARGET_LINK}")
    chain.reverse()
    return chain


@lru_cache(maxsize=4)
def _calibration(arm_id: str = "my_awesome_follower_arm") -> dict:
    p = LEROBOT_CALIB / "robots" / "so_follower" / f"{arm_id}.json"
    return json.loads(p.read_text())


def normalized_to_rad(joint: str, value: float, arm_id: str = "my_awesome_follower_arm") -> float:
    """RANGE_M100_100 归一化值 → 弧度(2048 ticks = 0 rad)。"""
    c = _calibration(arm_id)[joint]
    ticks = (value + 100.0) / 200.0 * (c["range_max"] - c["range_min"]) + c["range_min"]
    return (ticks - 2048.0) * (2 * np.pi / 4096.0)


def fk_pose(joint_norm: list[float], arm_id: str = "my_awesome_follower_arm") -> np.ndarray:
    """归一化关节值[6](含 gripper,忽略) → [x,y,z,qw,qx,qy,qz]。"""
    q = {n: normalized_to_rad(n, v, arm_id) for n, v in zip(FK_JOINTS, joint_norm[:5])}
    T = np.eye(4)
    for j in _chain():
        T = T @ _T(j["xyz"], j["rpy"])
        if j["type"] == "revolute" and j["name"] in q:
            T = T @ _axis_rot(j["axis"], q[j["name"]])
    return np.concatenate([T[:3, 3], _mat_to_quat(T[:3, :3])]).astype(np.float32)


def _mat_to_quat(R) -> np.ndarray:
    """旋转矩阵 → [qw,qx,qy,qz]。"""
    t = np.trace(R)
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        w, x, y, z = 0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w, x, y, z = (R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w, x, y, z = (R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s
    else:
        s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w, x, y, z = (R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s
    q = np.array([w, x, y, z])
    return q / np.linalg.norm(q)


def quat_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])


def quat_conj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def eef_series(states: np.ndarray, arm_id: str = "my_awesome_follower_arm"):
    """整条 episode 的归一化 observation.state[N,6] →
    (obs_eef[N,7] 绝对位姿, act_eef[N,7] 相邻帧增量;末帧 [0,0,0,1,0,0,0])。

    四元数做半球连续性对齐;增量为基座系:dp = p_{t+1}-p_t,dq = q_{t+1} ⊗ q_t^{-1}。
    """
    n = len(states)
    poses = np.stack([fk_pose(states[i], arm_id) for i in range(n)])
    for i in range(1, n):  # hemisphere continuity
        if np.dot(poses[i - 1, 3:], poses[i, 3:]) < 0:
            poses[i, 3:] = -poses[i, 3:]
    deltas = np.zeros((n, 7), dtype=np.float32)
    deltas[:, 3] = 1.0
    for i in range(n - 1):
        deltas[i, :3] = poses[i + 1, :3] - poses[i, :3]
        dq = quat_mul(poses[i + 1, 3:], quat_conj(poses[i, 3:]))
        if dq[0] < 0:
            dq = -dq
        deltas[i, 3:] = dq / np.linalg.norm(dq)
    return poses.astype(np.float32), deltas
