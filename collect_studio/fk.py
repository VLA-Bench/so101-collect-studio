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


def _mat_to_euler(R) -> np.ndarray:
    """旋转矩阵 → RPY(extrinsic xyz)弧度,与 `_rpy_to_R` 互逆。

    约定 `R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`,取值 [-pi, pi]。
    """
    roll = np.arctan2(R[2, 1], R[2, 2])
    pitch = -np.arcsin(np.clip(R[2, 0], -1.0, 1.0))
    yaw = np.arctan2(R[1, 0], R[0, 0])
    return np.array([roll, pitch, yaw])


def fk_pose(joint_norm: list[float], arm_id: str = "my_awesome_follower_arm") -> np.ndarray:
    """归一化关节值[6](含 gripper,忽略) → [x,y,z,roll,pitch,yaw]。"""
    q = {n: normalized_to_rad(n, v, arm_id) for n, v in zip(FK_JOINTS, joint_norm[:5])}
    T = np.eye(4)
    for j in _chain():
        T = T @ _T(j["xyz"], j["rpy"])
        if j["type"] == "revolute" and j["name"] in q:
            T = T @ _axis_rot(j["axis"], q[j["name"]])
    return np.concatenate([T[:3, 3], _mat_to_euler(T[:3, :3])]).astype(np.float32)


def eef_series(states: np.ndarray, actions: np.ndarray,
               arm_id: str = "my_awesome_follower_arm"):
    """整条 episode 的归一化关节序列 → (obs_eef[N,7], act_eef[N,7])。

    两者都是**绝对位姿** `[x,y,z,roll,pitch,yaw,gripper]`:
      - `obs_eef[t] = [FK(states[t]), states[t][5]]`(follower 实际位姿)
      - `act_eef[t] = [FK(actions[t]), actions[t][5]]`(leader 目标位姿)
    gripper 通道与该数据集关节第 6 维同值同单位(0–100 归一化),不做换算。
    """
    obs = np.stack([
        np.concatenate([fk_pose(states[i], arm_id), [states[i][5]]])
        for i in range(len(states))
    ]).astype(np.float32)
    act = np.stack([
        np.concatenate([fk_pose(actions[i], arm_id), [actions[i][5]]])
        for i in range(len(actions))
    ]).astype(np.float32)
    return obs, act
