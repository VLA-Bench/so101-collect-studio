# SO101 Collect Studio

浏览器端 SO101 数据采集工作台:一键校准导入、相机可视化绑定、翻书式批量采集、v2.1 + EEF 导出。
计划书与交互稿见 [docs/](docs/)。

## 启动

```bash
./run.sh          # 即 conda env `lerobot` 的 python -m collect_studio
# 打开 http://localhost:8600
```

首次在终端启动时 macOS 会弹「终端想访问摄像头」→ 允许(仅需一次)。

## 日常采集流程

1. **① 设备与校准**:串口按 USB 序列号自动识别(已预置)。点「导入校准并体检」(只复制文件 + 只读体检,永不重校准舵机)→「连接两臂」(连接后从动臂力矩是释放的)。
2. **② 相机绑定**:枚举序号(index)每次启动可能漂移,不用管 —— 绑定跟随硬件 uniqueID 持久化。首次使用**对着实时画面点角色按钮**(腕部/左后/右后)绑一次即可;内置相机自动排除;拔插相机后点「重新识别」。统一 640×480@30。
3. **③ 采集台**:左侧「开始遥操作」(从动臂会跟随主动臂当前位姿,先摆好主动臂!)。之后全键盘:
   - `Space` 开始/暂停录制,`Enter` 保存,`Backspace` 舍弃(录制中)/ 标废(历史页)
   - `←/→` 翻书浏览历史,`End` 回 LIVE,`G` 跳转,`T` 轮换任务,`E` 急停,`?` 帮助
   - 保存后台自动编码 mp4,可立刻录下一集;舍弃 = 删暂存目录,零成本
4. **④ 导出 v2.1**:录制先保存与版本无关的内部原始 episode(不是 v3),勾选任务/批次后直接生成 v2.1,没有 v3 → v2.1 转换。
   导出层的列命名与 EEF 格式**与 so101-nexus 仿真采集完全一致**(2026-07-18 统一改造;内部原始 parquet 列名不变,老 episode 仍可导出):
   - 关节列 `observation.pos_state` / `pos_action`:`float32[6]`,归一化值。
   - EEF 列 `observation.eef_state` / `eef_action`:`float32[7]`,均为**绝对位姿**
     `[x, y, z, roll, pitch, yaw, gripper]`,基座系,FK 自 `so101_new_calib.urdf` 的 `gripper_frame_link`。
     `eef_state` = FK(follower 关节),`eef_action` = **FK(leader action 关节)**(不是相邻帧增量,也不再从 state 序列差分)。
     欧拉角为 RPY(extrinsic xyz),`R = Rz(yaw)·Ry(pitch)·Rx(roll)`,取值 `[-π, π]`;
     gripper 通道与该数据集关节第 6 维同值同单位(0–100 归一化)。
   - 同时生成 GR00T `meta/modality.json`(所有条目显式写 `original_key`)与校验报告 `meta/validation_report.json`。

## 数据目录

```
~/so101_data/
├── staging/    录制暂存(舍弃即删;崩溃残留启动时提示)
├── library/<task>/<session>/<episode>/   已保存原始包(parquet+mp4+meta)
├── trash/      回收站(界面可恢复;清空需二次确认)
└── exports/    v2.1 数据集产物(纯读打包,可反复导出)
```

## 安全

- 应用内**没有**舵机重校准入口;校准只做文件复制。
- 连接 ≠ 上力矩;只有「开始遥操作」才上力矩;急停(`E`)= 停遥操作 + 断力矩。
- 换 USB 口后:串口按序列号自动重定位;相机 uniqueID 含 USB 拓扑位置,若换口只需在②页重新点一次角色。
