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
| `library.py` | 任务 / 批次(session, `YYYY-MM-DD_HHMM`)/ episode 管理；episode 编号全局自增 `episode_%06d`；回收站移动/清空；staging 残留清点 |
| `exporter.py` | v2.1 数据集导出（模块级单例任务 `JOB`，同时只允许一个导出）：拼接 parquet、注入 EEF、拷贝 MP4 不重编码、写 `meta/{info,modality,episodes,tasks,episodes_stats,validation_report}.json*` |
| `fk.py` | SO101 正运动学：自解析 `assets/so101_new_calib.urdf`（base → gripper_frame_link），归一化关节值经校准 JSON 反算 ticks → 弧度 → FK，纯 numpy 无 placo |

前端 `static/index.html`（约 900 行 vanilla JS）：四个页面步骤（①设备与校准 ②相机绑定 ③采集台 ④导出），键盘优先快捷键（Space/Enter/Backspace/E 等，见 README）。

## 数据流与目录约定

```
~/so101_data/
├── staging/<session>/<ep_id>/     # 录制暂存(data.jsonl + frames/<role>/*.jpg + meta.json)
├── library/<task_slug>/<session>/<ep_id>/   # 已保存(data.parquet + <role>.mp4 + meta.json)
├── trash/...                      # 标废,可恢复
└── exports/<name>/                # LeRobot v2.1 导出结果
```

- 先暂存后入库：「保存」才把 staging 晋升 library，「舍弃」= 删目录。
- 设备身份一律按序列号持久化：机械臂 = USB serial number，相机 = AVFoundation uniqueID；不依赖会漂移的 index/端口。
- 校准文件：`configs/calibration_backup/{robots/so_follower,teleoperators/so_leader}/*.json` 是用户自备的备份，"导入校准"仅复制到 LeRobot 缓存目录，文件名取 `devices.yaml` 里的 `id`。

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

- 唯一测试文件：`tests/test_camera_and_teleop.py`，用 `unittest` + mock 覆盖：相机枚举缓存与断流重建、遥操作启动时序（控制环就绪才算 on）、相机失败时报错需含角色与原因、前端不含 MJPEG 长连接。
- 运行：`uv run python -m unittest discover -s tests -v`。测试不触碰真实硬件（全部 Fake）。
- 没有 CI 配置；改动后请本地跑通测试，涉及硬件路径的改动需要真机冒烟（连接 → 遥操作 → 录制 → 导出）。

## 代码风格

- 注释、日志、docstring、面向用户的报错信息一律用**中文**（README 与文档也是中文）。commit message 用英文 conventional 风格（`feat:`/`fix:`/`docs:` 等，vendored 改动用 `[lerobot]` 前缀）。
- 命名直白：`ArmManager` / `CamManager` / `RecordService` 均为服务端单例；异常处理普遍 `except Exception  # noqa: BLE001` + `log.exception`。
- 注释里保留实测结论（如 AVFoundation 枚举顺序、OpenCV index 漂移），这类"为什么"注释不要删。

## 部署/运行环境说明

- 单机本地应用，监听 `127.0.0.1:8600`，无认证、无远程部署流程——不要把它暴露到非 loopback 地址。
- 首次启动 macOS 会请求摄像头权限；ffmpeg 必须已安装否则保存 episode 时编码失败。
- 真机使用流程见 `README.md`（摆动识别 → 导入校准并体检 → 连接两臂 → 绑定相机 → 采集 → 导出）。
