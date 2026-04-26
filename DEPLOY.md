# B站直播自动切片系统 - 部署文档

> 版本 1.0 | 最后更新: 2026-04-25

---

## 目录

1. [环境要求](#1-环境要求)
2. [安装步骤](#2-安装步骤)
3. [配置指南](#3-配置指南)
4. [启动与停止](#4-启动与停止)
5. [开机自启](#5-开机自启)
6. [目录结构](#6-目录结构)
7. [日志与排错](#7-日志与排错)
8. [更新维护](#8-更新维护)

---

## 1. 环境要求

| 组件 | 要求 | 说明 |
|------|------|------|
| **操作系统** | Windows 10/11 | |
| **Python** | ≥3.10 | 推荐 3.14+ |
| **FFmpeg** | 最新版 | 需添加到 PATH 或放在项目目录下 |
| **硬盘** | ≥50GB 空闲 | 直播录制文件较大（~1GB/小时/直播间） |
| **网络** | 稳定宽带 | B站直播流带宽消耗 ~5-10 Mbps/直播间 |
| **端口** | 5000 端口可用（可配置） | Web管理界面 |

---

## 2. 安装步骤

### 2.1 获取项目

```powershell
# 克隆或复制整个 bilibili-clipper 目录到你的机器
git clone <仓库地址>
# 或直接复制目录
```

项目结构见下文 [目录结构](#6-目录结构)。

### 2.2 安装 Python 依赖

```powershell
# 使用你的 Python 路径安装依赖
python -m pip install flask requests websocket-client pyyaml
```

如果下载慢可以用国内镜像：

```powershell
python -m pip install flask requests websocket-client pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.3 配置 FFmpeg

**方式一：系统 PATH（推荐）**

从 [gyan.dev FFmpeg Builds](https://www.gyan.dev/ffmpeg/builds/) 下载 `ffmpeg-release-full.7z`，解压后将 `bin` 目录添加到系统 PATH。

验证：

```powershell
ffmpeg -version
```

**方式二：放在项目目录**

在项目根目录下创建 `ffmpeg-portable/ffmpeg-master-latest-win64-gpl/bin/`，将 `ffmpeg.exe` 放入该目录。

> 项目目录下的 `webui.py` **已自动查找 FFmpeg**，按以下顺序：
> 1. `ffmpeg-portable/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe`（项目内便携版）
> 2. `ffmpeg.exe`（项目根目录）
> 3. 父级目录的便携版
> 4. 系统 PATH 中的 `ffmpeg`
> 5. 环境变量 `FFMPEG_PATH`
>
> 只要满足上述任一条件，无需手动修改代码。
> 如需强制指定路径，设置环境变量 `FFMPEG_PATH` 即可。

### 2.4 验证安装

```powershell
# 直接启动 Web 服务
python webui.py
```

启动后浏览器打开 http://localhost:5000，能看到页面即安装成功。

---

## 3. 配置指南

### 3.1 主配置文件

路径：项目根目录下的 `config/config.yaml`

```yaml
# === 直播间配置 ===
rooms:
  - room_id: xxxxx          # B站直播间数字ID
    anchor_name: "xxxxxx"  # 主播名称（用于目录结构和文件名）
    room_url: "https://live.bilibili.com/xxxxx"
    enable: true                  # 是否启用此直播间
    hot_keywords:                 # 弹幕高能关键词（可选）
      - "666"
      - "卧槽"
      - "牛逼"

  - room_id: xxxxx
    anchor_name: "xxxxx"
    room_url: "https://live.bilibili.com/xxxxx"
    enable: true

# === 自动化设置 ===
automation:
  check_interval: 30       # 直播状态轮询间隔（秒）
  auto_clip: true          # 下播后自动生成切片
  cleanup_old_files: false # 是否清理旧文件
  keep_days: 7             # 保留天数

# === 切片参数 ===
clipping:
  target_duration: 60      # 切片总目标时长（秒）
  min_clip_duration: 10    # 每个片段最短（秒）
  max_clip_duration: 30    # 每个片段最长（秒）
  buffer_before: 5         # 高能时刻前多截取（秒）
  buffer_after: 5          # 高能时刻后多截取（秒）

# === 高能时刻检测（核心！） ===
# 系统使用 HighEnergyDetector 进行多维度评分，
# 总分 = 弹幕 + 礼物 + SC + 进场爆发，超过 score_threshold 即触发切片
high_energy:
  score_threshold: 15.0     # 总分 ≥ 此值判定为高能时刻
                            # 调低 → 更容易触发切片，调高 → 只保留最精彩片段
  window_size: 5            # 滑动窗口宽度（秒），评估最近几秒的综合热度
  min_peak_interval: 30     # 高能时刻最小间隔（秒），避免连续重复触发

  # 各维度权重（可自由调整）
  weights:
    danmaku: 1.0          # 弹幕密度基础权重
                          # 弹幕得分 = max(0, 当前弹幕密度 - 阈值) × 此权重
    gift_small: 2.0       # 小礼物（辣条/小心心/荧光棒等），×数量
    gift_medium: 5.0      # 中礼物（舰长/提督/小电视/飞机等），×数量
    gift_large: 10.0      # 大礼物（总督/宇宙飞船等），×数量
    sc_small: 8.0         # 小额SC（≤50元），每单固定得分
    sc_medium: 15.0       # 中额SC（50~500元），每单固定得分
    sc_large: 30.0        # 大额SC（>500元），每单固定得分
    enter_burst: 3.0      # 用户进场爆发（超过1人/秒后，每人加此分数）

# === 弹幕分析 ===
danmaku_analysis:
  density_threshold: 10    # 弹幕密度阈值（条/秒）
                           # 只有超过此阈值，弹幕维度才开始计分
  # 建议：粉丝多/弹幕密的直播间（如大主播）可以把阈值调高（15~20），
  #       小主播或冷门游戏可以调低（5~8）

# === 存储路径与端口 ===
storage_root: ""           # 留空 = 项目目录下的 storage/
web_port: 5000             # Web管理界面端口
```

#### 高能检测评分机制说明

```
总分 = 弹幕得分 + 礼物得分 + SC得分 + 进场爆发得分

弹幕得分 = max(0, 当前弹幕密度(条/秒) - density_threshold) × danmaku权重
礼物得分 = 高级权重(小/中/大) × 数量 × 对应权重
SC得分   = 各条SC按金额分档加固定得分
进场得分 = max(0, 进场人数/秒 - 1) × enter_burst权重

当 总分 ≥ score_threshold 时，系统记录一个高能时刻
```

#### 调参建议

| 你的需求 | 调整方向 |
|----------|----------|
| 切片太多，想少出片 | **调高** `score_threshold`（如 20~30），或**调高** `min_peak_interval` |
| 切片太少，想多出片 | **调低** `score_threshold`（如 8~10），或**调低** `density_threshold` |
| 礼物/SC很重要 | **调高** `gift_large / sc_large` 权重，或调低弹幕维度让礼物更突出 |
| 只看弹幕爆发 | **调低**所有礼物/SC/进场权重到 0.1，只保留弹幕维度 |
| 观众进场爆发很关键 | **调高** `enter_burst` 权重（如 5~8） |

### 3.2 快速添加直播间

**方法一：编辑配置文件** — 在 `config/config.yaml` 的 `rooms:` 下新增一项：

```yaml
  - room_id: 12345678
    anchor_name: "主播名字"
    room_url: "https://live.bilibili.com/12345678"
    enable: true
```

**方法二：Web 界面添加** — 打开 http://localhost:5000，点击「添加」按钮，填房间号和主播名即可。

两种方式效果相同，Web 界面添加会自动保存到 `config/config.yaml`。

### 3.3 配置 FFmpeg

系统会自动按以下顺序查找 FFmpeg：

1. **项目目录内便携版** — `ffmpeg-portable/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe`
2. **项目根目录** — `ffmpeg.exe`
3. **父目录的便携版** — `../ffmpeg-portable/.../ffmpeg.exe`
4. **系统 PATH** 中的 `ffmpeg`
5. **环境变量** `FFMPEG_PATH` 可手动指定路径覆盖

如需手动指定，设置系统环境变量：

```powershell
$env:FFMPEG_PATH = "C:\your\path\ffmpeg.exe"
```

### 3.4 存储目录说明

```
storage/                          # 项目目录下的 storage（可自定义）
├── 主播名/               # 主播文件夹
│   ├── 2026_04_25_20_54_04_直播标题/  # 单场直播（开始时间_标题）
│   │   ├── record/               # 录制文件
│   │   │   └── 主播名_20260425_205405.ts
│   │   ├── clips/                # 切片文件
│   │   │   └── 00h05m20s_00h05m50s.mp4
│   │   └── logs/                 # FFmpeg日志
│   │       └── ffmpeg_21669525_20260425_205405.log
│   └── 2026_04_26_.../
├── 主播名/
│   └── ...
└── storage_root 可在 config.yaml 中自定义
```

#### 切片文件命名规则

每个切片文件以时间段命名，格式为：**`起始秒数_结束秒数.mp4`**

```
00h05m20s_00h05m50s.mp4    ← 从 5分20秒 到 5分50秒 的切片（共30秒）
01h02m15s_01h02m45s.mp4    ← 从 1小时2分15秒 到 1小时2分45秒 的切片（共30秒）
```

- 格式：`xxhxxmxxs_xxhxxmxxs.mp4`
- 时间戳相对于录制文件起始点（即直播开始时间）
- 方便快速定位对应时段，也支持在文件浏览器中按文件名排序

---

## 4. 启动与停止

### 4.1 一键脚本（推荐）

项目目录下提供了三个 `.bat` 脚本，双击即可运行：

| 脚本 | 功能 |
|------|------|
| `start.bat` | 启动服务（后台最小化窗口）|
| `stop.bat` | 只停止 webui.py 进程（不影响其他 Python 程序）|
| `restart.bat` | 一键重启（先停止 → 等待2秒 → 再启动）|

> 三个脚本都使用 `%~dp0` 自动定位到项目目录，双击即可运行，不受当前工作目录影响。

### 4.2 手动启动 Web 服务

```powershell
# 推荐：后台方式启动
Start-Process -FilePath "python" -ArgumentList "webui.py" -WorkingDirectory "项目目录的绝对路径" -WindowStyle Hidden

# 或者直接启动（会显示控制台日志）
python webui.py
```

启动后打开浏览器访问：**http://localhost:5000**

### 4.3 在 WebUI 中启动监控

1. 打开 http://localhost:5000
2. 页面顶部会显示所有房间的直播状态（即使未启动也会显示）
3. 点击顶栏「🚀 启动」按钮
4. 系统开始录制 + 弹幕抓取 + 高能检测
5. 下播后自动生成切片

### 4.4 停止服务

```powershell
# 通过 WebUI 先点「停止」再关闭窗口，或直接结束 Python 进程
Get-Process python* | Stop-Process -Force
```

### 4.5 一键重启

```powershell
Get-Process python* 2>$null | ForEach-Object { Stop-Process $_.Id -Force }
Start-Sleep -Seconds 2
Start-Process -FilePath "python" -ArgumentList "webui.py" -WorkingDirectory "项目目录的绝对路径" -WindowStyle Hidden
```

---

## 5. 开机自启

### 5.1 使用任务计划程序（推荐）

1. 打开「任务计划程序」(Task Scheduler)
2. 右侧「创建任务」
3. **常规** 选项卡：
   - 名称：`B站直播切片系统`
   - 勾选「不管用户是否登录都要运行」
   - 勾选「使用最高权限运行」
4. **触发器** 选项卡 → 新建 → 开始任务：「启动时」
5. **操作** 选项卡 → 新建：
   - 操作：`启动程序`
   - 程序或脚本：`python`
   - 添加参数：`webui.py`
   - 起始于：`项目目录的完整路径`
6. 确定 → 输入密码

### 5.2 使用 BAT 脚本（简单）

在项目目录下创建 `start_clipper.bat`：

```bat
@echo off
cd /d "%~dp0"
start /min "" python webui.py
```

将此 `.bat` 放入「启动」文件夹：
- 按 `Win + R`，输入 `shell:startup`
- 将 `start_clipper.bat` 放入即可

---

## 6. 目录结构

```
bilibili-clipper/             ← 项目根目录
├── webui.py                  # Web 管理界面（主入口）
├── live_monitor.py           # 直播状态监控模块
├── live_recorder.py          # 直播录制模块（FFmpeg + Python备用）
├── live_danmaku.py           # 实时弹幕抓取模块（WebSocket）
├── auto_clipper.py           # 自动切片生成模块
├── auto_live_clipper.py      # 主控制器（协调各模块）
├── auto_clipper_system.py    # 切片系统入口（旧版）
├── high_energy.py            # 多维度高能时刻检测器
├── config/
│   └── config.yaml           # 配置文件
├── templates/
│   └── index.html            # Web 前端页面
├── storage/                  # 录制和切片存储（默认）
│   └── <主播名>/
│       └── <开始时间_YYYY_MM_DD_HH_MM_SS>/
│           ├── record/       # 原始录制文件 (.ts)
│           ├── clips/        # 生成切片 (.mp4)
│           └── logs/         # FFmpeg日志
├── logs/                     # 系统运行日志
│   └── webui.log
├── DEPLOY.md                 # 部署文档（本文件）
├── MANUAL.md                 # 用户使用手册
└── ffmpeg-portable/          # 可选的 FFmpeg 便携版
```

**核心文件说明：**

| 文件 | 职责 | 修改频率 |
|------|------|----------|
| `webui.py` | **主入口**。Flask Web服务 + 监控主循环 | 偶尔更新 |
| `live_monitor.py` | 轮询B站API判断直播状态 | 很少修改 |
| `live_recorder.py` | FFmpeg/Python流式下载录制 | 很少修改 |
| `live_danmaku.py` | B站WebSocket弹幕协议连接 | 很少修改 |
| `high_energy.py` | 多维度高能时刻检测 | 很少修改 |
| `auto_clipper.py` | 根据高能时段剪辑视频 | 很少修改 |
| `config/config.yaml` | 所有配置（直播间、端口、参数） | **经常修改** |

---

## 7. 日志与排错

### 7.1 日志位置

| 日志 | 位置 | 内容 |
|------|------|------|
| **系统日志** | `logs/webui.log` | 运行信息、错误、状态 |
| **FFmpeg日志** | `storage/<主播名>/<session>/logs/ffmpeg_<room_id>.log` | FFmpeg输出 |
| **录制文件** | `storage/<主播名>/<session>/record/` | `.ts` 原始录制 |
| **切片段** | `storage/<主播名>/<session>/clips/` | `.mp4` 切片文件 |

### 7.2 常见问题

#### ❌ Web页面打不开（http://localhost:5000）

```powershell
# 检查是否在运行
Invoke-WebRequest -Uri 'http://127.0.0.1:5000/api/status' -UseBasicParsing
```

如果没输出，先确认 Python 和 Flask 已正确安装，然后启动服务。

#### ❌ 录制失败 / 文件0字节

1. 查看 FFmpeg 日志文件（`storage/.../logs/ffmpeg_*.log`）
2. 确认该直播间当前是否在直播
3. 检查 FFmpeg 路径是否正确配置
4. 重启服务

#### ❌ "ffmpeg 不是内部或外部命令"

`webui.py` 中的 `FFMPEG_PATH` 配置不正确。修改该变量为你的实际 FFmpeg 路径，详见 [3.3 配置 FFmpeg 路径](#33-配置-ffmpeg-路径重要)。

#### ❌ 弹幕显示已断开 (`connected: false`)

B站 WebSocket 连接有超时机制。弹幕少时会显示 `connected: false`，有弹幕时自动重新连接。

#### ❌ 切片生成失败

检查磁盘剩余空间。切片需要把录制文件重新编码，需要一定临时空间。

### 7.3 状态查看

访问以下 API 端点查看系统状态：

- `http://localhost:5000/api/status` — 系统总体状态（直播状态、录制信息）
- `http://localhost:5000/api/logs` — 最近日志
- `http://localhost:5000/api/files` — 存储目录文件浏览

---

## 8. 更新维护

### 8.1 更新系统代码

```powershell
# 先停止服务
Get-Process python* | Stop-Process -Force

# 备份配置
Copy-Item config\config.yaml config\config.yaml.bak

# 更新代码文件（替换 .py 和 .html 文件）
# 注意保留 config/config.yaml

# 重启服务
python webui.py
```

### 8.2 清理旧录制

手动删除不需要的录制和切片：

```powershell
# 删除指定主播的所有录制
Remove-Item storage\主播名\* -Recurse -Force
```

### 8.3 修改端口

编辑 `config/config.yaml` 中的 `web_port` 字段后重启服务。

### 8.4 性能参考

| 场景 | 资源占用 |
|------|----------|
| 仅状态监测（未启动录制） | CPU ~1%, 内存 ~50MB |
| 单个直播间录制 | CPU ~5-10%, 内存 ~200MB |
| 3个直播间同时录制 | CPU ~15-30%, 内存 ~500MB |
| 切片生成中 | CPU ~50-80%, 临时磁盘 2-5GB |
| 每小时录制文件 | ~1GB/直播间（原画1080p） |

---

> 如有问题请查看日志 (`logs/webui.log`) 或在 WebUI 的日志面板查看。
