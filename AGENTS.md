# AGENTS.md — SO101 Collect Studio

> 面向 AI 编码助手的项目说明。读者对本项目零先验知识。

## 项目概览

SO101 Collect Studio 是一个在浏览器中运行的 **SO-101 双臂数据采集工作台**，用于管理双臂遥操作（leader 主动臂 → follower 从动臂）、三路相机、episode 录制、历史回看，并导出 **LeRobot v2.1** 格式数据集（附 EEF 末端位姿与 GR00T `modality.json`）。

- **运行平台**：仅 macOS。相机采集走 AVFoundation（pyobjc），视频硬编用 `h264_videotoolbox`（ffmpeg）。
- **形态**：单体应用。FastAPI 后端 + 单个无构建的 vanilla HTML/JS 前端（`static/index.html`），REST 控制 + 轮询状态 + JPEG 单帧预览（无 WebSocket、无 MJPEG 长连接）。
- **Python 包非安装式**：`pyproject.toml` 里 `tool.uv.package = false`，代码以 `uv run python -m collect_studio` 直接运行。

## 技术栈与依赖

- Python ≥ 3.10，包管理用 **uv**（锁文件 `uv.lock`）。
- `lerobot[feetech]` **vendored 在 `third_party/lerobot`**（v0.4.4，editable path 依赖），驱动 Feetech STS3215 舵机总线。
- FastAPI + uvicorn + pydantic v2；opencv-python-headless；numpy 2；pyarrow；pyserial；pyyaml。
- pyobjc-framework-avfoundation / quartz / coremedia / libdispatch（macOS 相机直采）。
- 系统级依赖：**ffmpeg**（Homebrew 安装，`shutil.which("ffmpeg")` 或回退 `/opt/homebrew/bin/ffmpeg`）。

## 常用命令

```bash
uv sync                            # 按锁文件创建/同步 .venv(首次会拉取 LeRobot 及 ML 依赖)
uv run python -m collect_studio    # 启动服务:http://127.0.0.1:8600
uv run python -m unittest discover -s tests -v   # 运行测试(标准库 unittest,无 pytest 配置)
```

- 端口固定 **8600**（`collect_studio/__main__.py`）。
- `run.sh` 是作者本机的 legacy 启动脚本（硬编码了一个 anaconda 路径），**不要**当作通用入口；通用入口是上面的 `uv run`。
- 没有 lint/format CI 配置；测试用标准库 `unittest` + `unittest.mock`。

## 代码结构（`collect_studio/` 包）

| 模块 | 职责 |
|---|---|
| `__main__.py` | uvicorn 入口，host=127.0.0.1 port=8600 |
| `server.py` | 全部 REST 路由：状态、机械臂、相机、遥操作/录制、任务、episode 浏览、导出、静态前端。模块级单例 `arms / cams / rec` |
| `paths.py` | 所有路径常量。运行数据在 `~/so101_data/`（staging / library / trash / exports），LeRobot 校准缓存在 `~/.cache/huggingface/lerobot/calibration` |
| `config_store.py` | `configs/devices.yaml` 读写（带 mtime 缓存——录制热循环每帧都会调 `load()`，不能每次解析 YAML）；深合并默认值 |
| `arms.py` | **唯一允许 `import lerobot` 的模块**。`ArmManager`：串口识别（VID=0x1A86 QinHeng，按 USB serial number 绑定）、摆动识别 leader/follower、校准 JSON 复制导入、只读体检、连接（带重试）、力矩控制、急停 |
| `cams.py` | `CamManager`：AVFoundation 枚举（按 deviceType 区分内置/外置，不用名称猜）、角色绑定（wrist / left_rear / right_rear）持久化为 uniqueID、流生命周期管理、绑定健康检查 |
| `avf_capture.py` | `AVFCamStream`：用 `AVCaptureSession + deviceWithUniqueID:` 按 uniqueID 直采，绕开 OpenCV index（实测 index 与 uniqueID 会错位）；640×480，3 秒无帧看门狗标记异常。**必须**显式 `setActiveFormat_` + 用 `AVFrameRateRange` 自带 CMTime 钳帧率（否则 session 按最高 60fps 协商、单路 USB 带宽翻倍，三路同开时第三路收不到帧）；启动经模块级锁串行 + 3s 等首帧 + 整路重试 3 次 |
| `recorder.py` | `RecordService`：30Hz 遥操作控制环（读 leader → 写 follower → 取相机最新帧）与录制状态机（idle/rec/paused）解耦；JPEG 异步落盘；保存后后台 ffmpeg 编码 MP4 + 写 parquet，staging 晋升 library |
| `library.py` | 任务 / 批次(session, `YYYY-MM-DD_HHMM`)/ episode 管理；episode 编号全局自增 `episode_%06d`；回收站移动/清空、单集彻底删除（仅回收站）；episode meta 编辑（改 prompt / 归入已有任务并移目录）；staging 残留清点 |
| `exporter.py` | v2.1 数据集导出（模块级单例任务 `JOB`，同时只允许一个导出）：拼接 parquet、注入 EEF、拷贝 MP4 不重编码、写 `meta/{info,modality,episodes,tasks,episodes_stats,validation_report}.json*` |
| `fk.py` | SO101 正运动学：自解析 `assets/so101_new_calib.urdf`（base → gripper_frame_link），归一化关节值经校准 JSON 反算 ticks → 弧度 → FK，纯 numpy 无 placo |
| `scene_view.py` | 场景展示页后端（只读）：`parse_instruction` 从任务提示词文本正则解析资产配置（语法即 desk01 `build_instruction` 模板，作为**非场景任务的降级路径**）；布局/颜色常量本地硬编码；读写 `~/so101_data/current_display.json`（采集台上报的当前任务）；失败一律降级返回，不抛到路由层 |
| `scenes.py` | 场景权威逻辑 + 存储（迁移自 desk01 `scene.py`/`web/server.py`，2026-07-26 脱钩）：资产目录、`expand_scene`、`classify_targets`（T1–T5）、`build_instruction`、`validate_scene`、`box_centers`；`~/so101_data/scenes.json` 读写（mtime 缓存 + 原子写）；`save_scenes`（校验失败 `SceneValidationError`→400，重复资产 `SceneConflictError`→409 force 放行）；`sync_task_files()` 单向派生 `tasks_<场景id>.jsonl`（内容不变不重写，不删旧文件）；`instruction_index()` 供展示页精确匹配完整场景（含空盒） |

前端 `static/index.html`（约 1200 行 vanilla JS）：四个页面步骤（①设备与校准 ②相机绑定 ③采集台 ④数据管理与导出），键盘优先快捷键（Space/Enter/Backspace/E 等，见 README）。采集台右侧预览区只翻当前任务集合的 episode（`deckEps()` 按集合 slug 过滤，「全部」不过滤）；④是按「任务集合 → 任务 → 批次 → episode」浏览全部数据的管理器（预览 / 改提示词 / 标废恢复 / 彻底删除 + episode 级勾选导出）。采集台切换任务/集合时 fire-and-forget 上报 `POST /api/display/current`。

另有场景页 `static/scene.html`（`/scene`）双模式：**采集模式**（只读）轮询 `GET /api/display/scene`（1s），显示采集台当前任务与资产配置——命中 `scenes.json` 返回完整场景（含干扰空盒与派生子任务），未命中回退提示词解析（Three.js 3D 桌面摆放，CDN 失败降级为文字清单），不调用任何控制接口，可开在另一台电脑上；**制定模式**（`?mode=edit` 直达）迁移 desk01 web 三栏编辑器：左栏资产勾选 + 统计卡，中栏 3D（与采集模式共用一套构建函数），右栏场景 tabs + 任务卡片（targets 下拉、`/api/scenes/classify` 实时预览带 token 防乱序），保存走 `POST /api/scenes`（409 确认后 force 重发）。采集台 `S.taskSet` 命中 `status.scene_sets` 时禁用添加/导入按钮。

## 数据流与目录约定

```
~/so101_data/
├── staging/<session>/<ep_id>/     # 录制暂存(data.jsonl + frames/<role>/*.jpg + meta.json)
├── library/<set>/<slug>/<session>/<ep_id>/   # 已保存(data.parquet + <role>.mp4 + meta.json)
├── trash/...                      # 标废,可恢复
├── exports/<name>/                # LeRobot v2.1 导出结果
├── scenes.json                    # 场景与场景任务的唯一权威数据源(collapse 形式)
└── tasks_<场景id>.jsonl           # 由 scenes.json 单向同步派生的只读任务集合
```

- 先暂存后入库：「保存」才把 staging 晋升 library，「舍弃」= 删目录。
- 设备身份一律按序列号持久化：机械臂 = USB serial number，相机 = AVFoundation uniqueID；不依赖会漂移的 index/端口。
- 校准文件：`configs/calibration_backup/{robots/so_follower,teleoperators/so_leader}/*.json` 是用户自备的备份，"导入校准"仅复制到 LeRobot 缓存目录，文件名取 `devices.yaml` 里的 `id`。
- 任务集合：`~/so101_data/tasks.json` 为「默认」集合，`tasks_<名称>.json/.jsonl` 为额外集合；**任务身份 = (task_set, task_slug)**，library 四层目录 `library/<set>/<slug>/<session>/<ep>`；每个文件**双格式兼容**——JSON 数组（`[{"prompt": ...}]`，可带 `slug`）或 LeRobot `tasks.jsonl`（每行 `{"task_index","task"}`），同名冲突时 `.json` 优先，新写默认 `.json`。**slug 不必手写**：缺省由 `_slugify(prompt)` 派生，文件里显式写的 slug 原样保留；写回按原文件格式。`library.load_tasks()` 合并全部集合（**不跨集合去重**：同 slug 在不同集合是不同任务）并带 `set` 字段，`load_tasks_grouped()` 提供按集合分组视图（`/api/status` 的 `tasks_by_set`）。
- **场景派生集合只读**：`tasks_<场景id>.jsonl` 由 `scenes.sync_task_files()` 从 `scenes.json` 单向生成（内容不变不重写，**不删除**任何旧 tasks 文件——废弃集合连同已录数据由用户手动清理）；`POST /api/tasks(/import)` 对场景集合返回 400，采集台前端同步禁用添加/导入；编辑入口在 `/scene` 制定模式。`configs/scenes_backup/scenes_desk01_20260726.json` 是迁移自 desk01 时的只读备份。

## 硬性边界与安全约定（改代码必须遵守）

1. **LeRobot 边界**（源自 `CLAUDE.md`，仍然有效）：
   - `import lerobot` 只允许出现在 `collect_studio/arms.py`；其他模块必须经 `ArmManager` 获取机械臂能力。
   - exporter 只对齐数据格式，**不得** import lerobot。
2. **绝不触碰舵机校准**：代码中没有任何调用 `calibrate()` 的路径，`connect` 一律 `calibrate=False`。不要新增重校准入口。
3. **力矩安全**：连接后立即 `disable_torque`；只有显式 `start_teleop` 才 `enable_torque`；`stop_teleop` 刻意保持力矩（防从动臂跌落），释放力矩走急停。控制环异常时自动急停。
4. 录制热路径上的 `config_store.load()` 依赖 mtime 缓存，不要绕过缓存或改成每次读盘。
5. AVFoundation 回调（`_SampleSink`）跑在 dispatch 线程，**绝不能抛异常**（现有代码用 `# noqa: BLE001` 宽捕获就是这个原因）。

## Vendored LeRobot 维护规则

- `third_party/lerobot` 通过 git subtree 引入。修改它的 commit message 必须用 `[lerobot]` 前缀，且不得与业务改动混在同一 commit。
- 每项本地修改必须登记到 `docs/lerobot-patches.md`（表格：日期 / 文件 / 原因 / 可否回馈上游）。当前表为空 = 暂无本地 patch。
- 升级上游流程见 `CLAUDE.md`（`git subtree pull --squash` → 解冲突 → 冒烟 → 真机验证 teleop/录制/导出）。

## 测试

- 测试文件在 `tests/` 下，用 `unittest` + mock，全部不触碰真实硬件：
  - `test_camera_and_teleop.py`：相机枚举缓存与断流重建、遥操作启动时序（控制环就绪才算 on）、相机失败时报错需含角色与原因、前端不含 MJPEG 长连接。
  - `test_task_sets.py`：任务集合枚举（含 `.jsonl`）、json/jsonl 双格式解析、slug 派生与显式保留、集合内查重、场景派生集合添加/导入被拒（mock scenes）。
  - `test_tasks_import.py`：导入的格式识别、重复跳过、坏行不落盘。
  - `test_export_selection.py`：导出选择（任务 / 批次 / episode id / 空 = 全部已保存）与按任务汇总。
  - `test_episode_manage.py`：`update_episode`（改 prompt / 归入任务移目录 / 校验）与 `delete_episode`（仅回收站）。
  - `test_scene_display.py`：`parse_instruction` 文本解析、scenes.json 精确匹配（完整场景含空盒与 subtasks）与解析降级两路径、`POST /api/display/current` → `GET /api/display/scene` 链路（直接调路由处理函数，环境无 httpx 故不用 TestClient）。
  - `test_scenes.py`：迁移黄金数据（`configs/scenes_backup/scenes_desk01_20260726.json`）指令生成→解析往返一致、`validate_scene` 各错误分支、`save_scenes` 校验失败/重复资产 409/force 放行、`sync_task_files` 生成 `tasks_A.jsonl` 与内容不变不重写。
- 运行：`uv run python -m unittest discover -s tests -v`。测试不触碰真实硬件（全部 Fake）。
- 没有 CI 配置；改动后请本地跑通测试，涉及硬件路径的改动需要真机冒烟（连接 → 遥操作 → 录制 → 导出）。

## 代码风格

- 注释、日志、docstring、面向用户的报错信息一律用**中文**（README 与文档也是中文）。commit message 用英文 conventional 风格（`feat:`/`fix:`/`docs:` 等，vendored 改动用 `[lerobot]` 前缀）。
- 命名直白：`ArmManager` / `CamManager` / `RecordService` 均为服务端单例；异常处理普遍 `except Exception  # noqa: BLE001` + `log.exception`。
- 注释里保留实测结论（如 AVFoundation 枚举顺序、OpenCV index 漂移），这类"为什么"注释不要删。

## 部署/运行环境说明

- 单机本地应用，默认监听 `127.0.0.1:8600`，无认证、无远程部署流程。可用环境变量 `SO101_HOST=0.0.0.0 uv run python -m collect_studio` 放开局域网访问（供另一台电脑打开只读展示页 `/scene`）——**无认证，全部控制接口随之暴露，仅在可信局域网开启**；不要把它暴露到非可信地址。
- 首次启动 macOS 会请求摄像头权限；ffmpeg 必须已安装否则保存 episode 时编码失败。
- 真机使用流程见 `README.md`（摆动识别 → 导入校准并体检 → 连接两臂 → 绑定相机 → 采集 → 导出）。
