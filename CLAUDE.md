# Repository maintenance rules

## LeRobot boundary

- `lerobot` import 只允许出现在 `collect_studio/arms.py`。
- 其他模块必须经 `ArmManager` 获取机械臂能力。
- exporter 只对齐数据格式，不得 import `lerobot`。

## Vendored changes

- 修改 `third_party/lerobot` 的 commit message 必须使用 `[lerobot]` 前缀。
- vendored LeRobot 修改不得与业务改动混在同一个 commit 中。
- 每项本地修改都必须登记到 `docs/lerobot-patches.md`。

## Upstream upgrade

```bash
git remote add lerobot-seeed https://github.com/Seeed-Projects/lerobot.git   # 或 HF 官方
git fetch lerobot-seeed <branch-or-tag>
git subtree pull --prefix third_party/lerobot lerobot-seeed <ref> --squash
# 解冲突 → 冒烟 → 真机验证 teleop/录制/导出
```
