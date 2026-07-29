"""夹爪堵转保护的纯状态机，无硬件依赖。"""

from __future__ import annotations

import math
from collections import deque


DEFAULT_GRIPPER_PROTECTION = {
    "enabled": True,
    "current_limit_ma": 300.0,
    "stall_window_ms": 300,
    "max_position_change": 0.5,
    "min_closing_error": 2.0,
    "release_margin": 2.0,
}


def validate_gripper_config(config: dict) -> dict:
    """校验并返回类型稳定的夹爪保护配置。"""
    if not isinstance(config.get("enabled"), bool):
        raise ValueError("enabled 必须是布尔值")
    values = {
        key: float(config[key])
        for key in (
            "current_limit_ma",
            "stall_window_ms",
            "max_position_change",
            "min_closing_error",
            "release_margin",
        )
    }
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("夹爪保护参数必须是有限数字")
    if values["current_limit_ma"] <= 0:
        raise ValueError("保护电流必须大于 0")
    if values["stall_window_ms"] <= 0:
        raise ValueError("停滞窗口必须大于 0")
    for key in ("max_position_change", "min_closing_error", "release_margin"):
        if not 0 <= values[key] <= 100:
            raise ValueError(f"{key} 必须在 [0, 100] 内")
    return {
        "enabled": config["enabled"],
        "current_limit_ma": values["current_limit_ma"],
        "stall_window_ms": values["stall_window_ms"],
        "max_position_change": values["max_position_change"],
        "min_closing_error": values["min_closing_error"],
        "release_margin": values["release_margin"],
    }


class GripperProtection:
    """持续高电流且位置不动时，锁住夹爪继续闭合的目标。"""

    def __init__(self, config: dict):
        self.active = False
        self.hold_position: float | None = None
        self._candidate: deque[tuple[float, float]] = deque()
        self.config: dict = {}
        self.configure(config)

    def configure(self, config: dict) -> None:
        """热更新参数；数值更新保留锁定，禁用则解除。"""
        was_active = self.active
        hold_position = self.hold_position
        self.config = validate_gripper_config(config)
        self._candidate.clear()
        if not self.config["enabled"]:
            self.active = False
            self.hold_position = None
        elif was_active:
            self.active = True
            self.hold_position = hold_position

    def reset(self) -> None:
        self.active = False
        self.hold_position = None
        self._candidate.clear()

    def _metrics(self, now: float) -> tuple[float, float | None]:
        if not self._candidate:
            return 0.0, None
        candidate_ms = max(0.0, (now - self._candidate[0][0]) * 1000.0)
        positions = [position for _, position in self._candidate]
        return candidate_ms, max(positions) - min(positions)

    def _state(
        self,
        requested: float,
        position: float,
        current_ma: float | None,
        now: float,
        applied: float,
    ) -> dict:
        candidate_ms, position_span = self._metrics(now)
        return {
            "position": position,
            "requested_target": requested,
            "applied_target": applied,
            "current_ma": current_ma,
            "protection_active": self.active,
            "hold_position": self.hold_position,
            "candidate_ms": candidate_ms,
            "position_span": position_span,
            "config": dict(self.config),
        }

    def snapshot(self) -> dict:
        return {
            "position": None,
            "requested_target": None,
            "applied_target": None,
            "current_ma": None,
            "protection_active": self.active,
            "hold_position": self.hold_position,
            "candidate_ms": 0.0,
            "position_span": None,
            "config": dict(self.config),
        }

    def apply(
        self,
        requested: float,
        position: float,
        current_ma: float | None,
        now: float,
    ) -> tuple[float, dict, str | None]:
        requested = float(requested)
        position = float(position)
        current = None if current_ma is None else float(current_ma)

        if not self.config["enabled"]:
            self.reset()
            return requested, self._state(requested, position, current, now, requested), None

        if self.active:
            hold = float(self.hold_position)
            if requested >= hold + self.config["release_margin"]:
                self.reset()
                return (
                    requested,
                    self._state(requested, position, current, now, requested),
                    "released",
                )
            return hold, self._state(requested, position, current, now, hold), None

        closing_requested = requested < position - self.config["min_closing_error"]
        current_high = current is not None and current >= self.config["current_limit_ma"]
        if closing_requested and current_high:
            self._candidate.append((now, position))
            cutoff = now - self.config["stall_window_ms"] / 1000.0
            # 保留截止点之前的最后一个样本，确保窗口覆盖时长判断准确。
            while len(self._candidate) >= 2 and self._candidate[1][0] <= cutoff:
                self._candidate.popleft()
            candidate_ms, position_span = self._metrics(now)
            if (
                candidate_ms >= self.config["stall_window_ms"]
                and position_span is not None
                and position_span <= self.config["max_position_change"]
            ):
                self.active = True
                self.hold_position = position
                return (
                    position,
                    self._state(requested, position, current, now, position),
                    "activated",
                )
        else:
            self._candidate.clear()

        return requested, self._state(requested, position, current, now, requested), None
