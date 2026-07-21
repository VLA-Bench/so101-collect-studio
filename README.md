# SO101 Collect Studio

SO101 Collect Studio 是一个在浏览器中运行的 SO-101 数据采集工作台，用于管理双臂遥操作、三路相机、episode 录制、历史回看和 LeRobot v2.1 数据集导出。

## 使用前准备

当前版本面向 macOS，使用 AVFoundation 识别和采集相机。开始前请准备：

- 一台安装了 [Homebrew](https://brew.sh/) 的 Mac
- 一套 SO-101 主动臂（leader）和从动臂（follower）
- 两个已经完成 LeRobot 校准的机械臂及其校准 JSON 文件
- 最多三台 UVC 相机，分别用于腕部、左后和右后视角
- 可用的 USB 串口和相机访问权限

应用不会执行舵机重校准。没有自己设备的有效校准文件时，请先在标准 LeRobot 流程中完成校准，再使用本项目。

## 1. 获取代码

```bash
git clone https://github.com/VLA-Bench/so101-collect-studio.git
cd so101-collect-studio
```

仓库已包含所需的 LeRobot 源码，不需要另外克隆 LeRobot。

## 2. 安装环境

安装 [uv](https://docs.astral.sh/uv/) 和 ffmpeg：

```bash
brew install uv ffmpeg
```

根据锁文件创建 Python 环境并安装依赖：

```bash
uv sync
```

项目要求 Python 3.10 或更高版本。uv 会根据锁文件管理项目环境。首次同步需要下载 LeRobot 及其机器学习依赖，耗时和磁盘占用取决于网络环境。

## 3. 配置机械臂和相机

### 3.1 放入自己的校准文件

将已经验证过的校准 JSON 分别放到：

```text
configs/calibration_backup/
├── robots/so_follower/          # 从动臂校准 JSON
└── teleoperators/so_leader/     # 主动臂校准 JSON
```

清除目录中的示例文件，并确保每个目录只保留一个与当前机械臂对应的 JSON 文件。不要在不同机械臂之间复用校准文件。

### 3.2 编辑设备配置

打开 `configs/devices.yaml`，为自己的设备设置校准 ID。若尚不知道 USB 序列号，可先将 `serial` 留空，启动后通过页面的“摆动识别”自动写入：

```yaml
arms:
  leader:
    serial: ''
    id: my_leader_arm
  follower:
    serial: ''
    id: my_follower_arm
cameras:
  wrist: null
  left_rear: null
  right_rear: null
record:
  fps: 30
  width: 640
  height: 480
```

`leader.id` 和 `follower.id` 会成为校准文件导入 LeRobot 缓存后的名称。相机项可以保持为 `null`，之后在页面中通过实时画面绑定；绑定会按 AVFoundation uniqueID 保存，不依赖可能变化的相机 index。

## 4. 启动应用

```bash
uv run python -m collect_studio
```

浏览器打开 [http://127.0.0.1:8600](http://127.0.0.1:8600)。首次启动时，macOS 可能询问终端的摄像头权限，请选择允许。

停止应用时在启动终端按 `Ctrl+C`。

## 5. 首次使用

页面按四个步骤完成采集。

### ① 设备与校准

1. 接入主动臂和从动臂，确认系统能看到两个控制板串口。
2. 如果 `devices.yaml` 中没有正确序列号，点击“开始摆动识别”，并在提示期间用手晃动主动臂。识别要求恰好检测到两个候选串口。
3. 点击“导入校准并体检”。应用只会复制你放入的校准文件并读取当前位置，不会重校准或主动移动舵机。
4. 体检通过后点击“连接两臂”。连接完成时从动臂仍处于释放力矩状态。

### ② 相机绑定

1. 根据实时画面确认每台相机的位置。
2. 将相机分别绑定为 `wrist`、`left_rear` 和 `right_rear`。
3. 如果拔插或更换了相机，点击“重新识别相机”后重新确认绑定。

应用默认不打开 Mac 内置相机。若需要使用或排查内置相机，可在相机卡片上手动启动预览。

### ③ 采集台

1. 新建或选择一个任务。任务文件支持双格式：JSON 数组（`[{"prompt": "..."}]`，可带 `slug`）或 LeRobot `tasks.jsonl`（每行 `{"task_index": n, "task": "..."}`）；`slug` 一律由代码按 prompt 自动派生，不必手写。因此可以**直接把 `tasks.json` 或 `tasks.jsonl` 拷入 `~/so101_data/`**（`tasks.json`/`tasks.jsonl` 为「默认」集合，`tasks_<名称>.json/.jsonl` 为额外集合，同名时 `.json` 优先），无需导入；也可以点任务面板上的「导入」按钮把别的文件内容并入当前集合，重复任务自动跳过。任务列表按「已采集 / 未采集」分组排序，编号 `#N` 固定为任务在文件中的顺序号（不随采集进度重排）；两组默认都收起（各显示前 2 个），组标题行右侧有本组的「展开/收起」和独立的「▲/▼」正倒序迷你按钮；采集台顶部的下拉框可切换某个集合或「全部」。
2. 右侧预览区只翻**当前任务集合**的 episode（选「全部」则不过滤；底部缩略条标签显示当前集合名）；切换任务集合自动回到 LIVE，切换任务（点击或 `T`）也会回到 LIVE。
3. 开始遥操作前，先把主动臂摆到接近从动臂的姿态。
4. 点击“开始遥操作”，确认三路画面和机械臂状态正常。
5. 使用按钮或快捷键录制 episode：

| 快捷键 | 功能 |
|---|---|
| `Space` | 开始、暂停或恢复录制 |
| `Enter` | 保存当前 episode |
| `Backspace` | 舍弃当前录制；历史页中标废或恢复 episode |
| `←` / `→` | 浏览历史 episode |
| `End` | 返回 LIVE 页面 |
| `G` | 跳转到指定 episode |
| `T` | 轮换任务 |
| `E` | 急停：停止遥操作并释放从动臂力矩 |
| `?` | 打开快捷键帮助 |

保存后，视频会在后台编码为 MP4，可以直接开始下一集录制。

### ④ 数据管理与导出

导出页是一个面向全部数据（含回收站）的文件管理器：左栏按「任务 → 批次」浏览，中栏列出对应 episode，可逐行预览三路视频、修改提示词（可选同时归入另一个已有任务，会自动移动目录）、标废/恢复、彻底删除（仅回收站）。

勾选要导出的 episode（可用「全选本节点」按任务/批次批量勾选；**一个不勾 = 导出全部已保存**），填写数据集名称后开始导出。导出不会修改原始 episode，可以重复执行。

导出结果包含：

- LeRobot v2.1 数据集结构
- `observation.pos_state` 和 `pos_action` 关节数据
- `observation.eef_state` 和 `eef_action` 绝对末端位姿
- GR00T `meta/modality.json`
- `meta/validation_report.json` 校验报告

## 数据目录

运行数据默认保存在当前用户主目录：

```text
~/so101_data/
├── staging/    # 正在录制或异常中断后留下的暂存数据
├── library/    # 已保存的原始 episode
├── trash/      # 可恢复的标废 episode
└── exports/    # 导出的 LeRobot v2.1 数据集
```

删除暂存录制或清空回收站会移除对应数据；导出操作只读取 `library` 中的原始 episode。

## 常见问题

- **页面打不开**：确认启动终端仍在运行，并访问 `http://127.0.0.1:8600`。
- **相机没有画面**：检查 macOS 摄像头权限，然后在相机页点击“重新识别相机”。
- **找不到机械臂**：检查 USB 连接，并确认两个控制板都能被识别为 QinHeng/CH34x 串口设备。
- **摆动识别失败**：识别时只能连接两个候选机械臂串口，并需要持续晃动主动臂。
- **校准导入失败**：确认两个校准备份目录各有一个 JSON，且 `devices.yaml` 中的 ID 非空。
- **视频编码失败**：运行 `ffmpeg -version`，确认 ffmpeg 已正确安装。
- **8600 端口被占用**：关闭占用该端口的进程后重新启动应用。

## 安全说明

- 应用没有舵机重校准入口；校准导入只复制文件。
- 连接机械臂不等于开启力矩，只有开始遥操作时才会启用从动臂力矩。
- 开始遥操作前应让两臂姿态尽量接近，避免从动臂突然追随较远目标。
- 出现异常时按 `E` 或页面右上角“急停”，停止遥操作并释放从动臂力矩。
- 体检异常时先检查接线、校准文件和关节位置，不要继续录制。
