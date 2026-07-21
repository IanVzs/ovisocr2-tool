# OCR工具（区域截图 OCR）

系统托盘小工具：点击后框选屏幕区域 → 用 [HunyuanOCR](https://github.com/Tencent-Hunyuan/HunyuanOCR) 解析为 Markdown → 写入系统剪贴板，并在托盘任务面板查看进度。

## 架构（分前后端，但都在本机、由应用自管）

本工具逻辑上分两层，运行时是**同机两个进程**，不是需要分开部署的 C/S：

```
┌─────────────────────────────┐        HTTP (localhost)        ┌──────────────────────────┐
│  前端 / 应用层（本 Python 包）  │  ──  POST /v1/chat/completions ──▶ │  后端 / 推理层 llama-server │
│  · 系统托盘 + 任务面板 UI       │                                │  · llama.cpp 跑 GGUF        │
│  · Spectacle 区域截图          │  ◀── SSE 流式返回 OCR 文本 ──       │  · 监听 127.0.0.1:8080      │
│  · wl-copy 写剪贴板 / 通知      │                                │  · Vulkan 核显 / CPU 推理    │
│  · HTTP 客户端                │                                └──────────────────────────┘
└─────────────────────────────┘                                     ▲ 由前端自动拉起/探活/关闭
```

- **前端**：`ocr_tool` 这个 Python 包（托盘进程），只做截图、UI、剪贴板、HTTP 请求。
- **后端**：`llama-server`（llama.cpp），本机子进程，**应用启动时自动拉起、就绪后才截图、退出时自动关闭**。
- 也可让应用连接**外部已运行**的服务（设 `OCR_TOOL_SERVER_URL`），此时应用不代管其生命周期。

## 模型与存储路径

| 项 | 内容 |
|----|------|
| 模型 | [HunyuanOCR-1.5](https://github.com/Tencent-Hunyuan/HunyuanOCR)（腾讯混元，端到端 OCR 视觉语言模型，~1B 参数） |
| GGUF 来源 | 官方 [`ggml-org/HunyuanOCR-GGUF`](https://huggingface.co/ggml-org/HunyuanOCR-GGUF)（**必须官方**，见下方注意） |
| 量化 | Q8_0 |
| 文本模型 | `~/.local/share/ocr-tool/models/HunyuanOCR-Q8_0.gguf`（~578MB） |
| 视觉编码器(mmproj) | `~/.local/share/ocr-tool/models/mmproj-HunyuanOCR-Q8_0.gguf`（~733MB） |
| 推理后端二进制 | `~/.local/share/ocr-tool/llama-bin/<release>/llama-server`（预编译，默认 Vulkan 版） |
| `-hf` 兜底下载缓存 | `~/.cache/llama.cpp/`（仅当本地无 GGUF、走在线拉取时） |

模型与后端合计约 **1.3GB**，统一放在**共享数据目录** `~/.local/share/ocr-tool/`
（遵循 `XDG_DATA_HOME`，可用 `OCR_TOOL_DATA_DIR` 覆盖）。**`make run`（调试）与独立安装版
（`make install-app`）读同一份**，不会重复下载。旧版曾放在项目内 `third_party/`，仍作兼容回退。

### 模型加载 / 使用切换策略

应用启动预热时按下面顺序决定「用哪个后端、哪个模型、在哪算」：

1. **后端二进制**：`LLAMA_SERVER_BIN` → PATH 上的 `llama-server` → `third_party/llama-bin/**/llama-server`。
2. **模型文件**：`OCR_TOOL_MODEL_PATH`/`OCR_TOOL_MMPROJ_PATH` 显式指定 → 否则自动发现 `third_party/models/*.gguf`（非 `mmproj` 者为主模型，`mmproj` 者为视觉编码器）→ 都没有则用 `-hf OCR_TOOL_HF_REPO` 在线拉取。
3. **计算设备**：默认 `-ngl 99`（Vulkan 核显全量 offload）；**GPU 初始化失败自动回退纯 CPU** 重启一次（`OCR_TOOL_NGL=0` 可强制 CPU）。
4. **后端变体**：安装时 `OCR_TOOL_LLAMA_VARIANT=vulkan|cpu` 决定 `make install-llama` 下载哪个预编译包（默认 `vulkan`）。
5. **服务生命周期**：默认应用自管（拉起/探活/关闭）；设 `OCR_TOOL_SERVER_URL` 则只连外部服务。
6. **任务/提示词**：`OCR_TOOL_TASK`（默认 `doc_parse`，输出 Markdown），提示词用 HunyuanOCR 官方固定词，不开放自定义（改词会掉点）。

> ⚠️ 必须用官方 `ggml-org/HunyuanOCR-GGUF`。第三方旧 GGUF 的投影层类型 `hunyuanocr`
> 在新版 llama.cpp（b9263+）已并入 HunyuanVL，会报 `unknown projector type: hunyuanocr`。

## 系统依赖

| 用途 | Fedora 包 / 命令 |
|------|----------------|
| 区域截图 | `spectacle`（`sudo dnf install spectacle`） |
| 剪贴板 (Wayland) | `wl-copy`（`sudo dnf install wl-clipboard`） |
| 桌面通知 | `notify-send`（`libnotify`） |
| 托盘 UI | `PySide6`（`pip install` 时自动安装） |
| 推理后端 | `llama-server`（llama.cpp b8670+，支持 HunyuanOCR 视觉，由 `make install-llama` 下载预编译版） |

本机需 **Python ≥ 3.10**。

## 安装

```bash
cd ~/桌面/ovisocr2-tool
make install         # 装 Python 依赖 + editable + 桌面入口
make install-llama   # 下载预编译 llama-server + HunyuanOCR 官方 GGUF（免编译，省内存）
```

`make install-llama` **不本地编译**（多进程 g++ 会打爆内存），而是：

- 从 llama.cpp GitHub release 下载 **Vulkan 版**预编译 `llama-server`
  → `~/.local/share/ocr-tool/llama-bin/`（核显加速；无 GPU 的机器上会自动跑 CPU）
- 从官方 `ggml-org/HunyuanOCR-GGUF` 下载 Q8_0 文本模型 + mmproj 视觉编码器（共约 **1.3GB**）
  → `~/.local/share/ocr-tool/models/`

**核显加速**：默认 Vulkan 版 + `-ngl 99` 全量 offload。实测 AMD 核显(RADV)上一屏文字冷启动
约 3~5s（纯 CPU 约 11~13s）。若 Vulkan 初始化失败会自动回退纯 CPU，不会中断。
纯 CPU 机器（连 vulkan loader 都没有）可改装 CPU 版：

```bash
OCR_TOOL_LLAMA_VARIANT=cpu make install-llama
```

（模型仓库为何必须用官方版、加载/切换策略详见上文「模型与存储路径」。）

应用会自动发现共享数据目录下的 `llama-server` 与模型。若已有自己的 `llama-server`，可用环境变量指定：

```bash
export LLAMA_SERVER_BIN=/path/to/llama-server
```

### 两种安装方式

- **开发/调试**：`make install`（editable）+ `make run`——依赖本源码目录，改代码即时生效。
- **独立安装（不依赖源码）**：`make install-app`——用 PyInstaller 打成自带运行时的独立包，
  装到 `~/.local/lib/ocr-tool/` 并注册桌面入口；删掉源码目录也能从菜单/图标启动。详见「打包 / 独立安装」。

桌面入口：

- `~/.local/share/applications/ocr-tool.desktop`
- `~/桌面/ocr-tool.desktop`（若存在「桌面」目录）

## 使用

```bash
make run          # 若托盘未运行则启动并截图；已运行则提交新截图
make status       # 查看托盘任务列表
make quit         # 退出托盘应用
make serve        # 手动前台跑 llama-server（调试用）
make show-version
make set-version VERSION=0.2.1
make help
```

或从应用菜单 / 桌面图标启动「OCR工具」。

**托盘交互：**

- 左键点击托盘图标 → 在光标处弹出任务面板（最多 9 条，含进度条）
- 面板**失焦 / 点击外部 / 按 Esc** 自动关闭
- OCR 任务可「查看原图」「复制文本」（完成后且有文本时复制可用）
- 右键菜单：新截图识别 / 显示任务列表 / 退出

> 界面默认跑在 XWayland(xcb) 上，以便窗口能精确定位到光标并正常响应失焦。
> 可用 `QT_QPA_PLATFORM` 覆盖（如强制 `wayland`）。

流程：框选 → 托盘显示任务进度 → 完成后结果自动写入剪贴板并通知。取消框选则静默退出。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLAMA_SERVER_BIN` | 自动发现 | `llama-server` 可执行文件路径 |
| `OCR_TOOL_SERVER_URL` | 空 | 指定外部已运行的服务地址；设置后本应用不代管其生命周期 |
| `OCR_TOOL_PORT` | `8080` | 自管服务的端口 |
| `OCR_TOOL_HF_REPO` | `ggml-org/HunyuanOCR-GGUF` | `-hf` 兜底拉取的 GGUF 仓库（本地已有则不用） |
| `OCR_TOOL_MODEL_PATH` / `OCR_TOOL_MMPROJ_PATH` | 自动发现共享数据目录 | 本地 GGUF 文件路径 |
| `OCR_TOOL_DATA_DIR` | `~/.local/share/ocr-tool` | 模型/后端共享数据目录（dev 与安装版共用） |
| `OCR_TOOL_MAX_TOKENS` | `4096` | 生成上限 |
| `OCR_TOOL_MAX_SIDE` | `2048` | 送入前图像最长边缩放上限（越小越快） |
| `OCR_TOOL_TASK` | `doc_parse` | 任务类型：`doc_parse`/`structured_parse`/`formula`/`table`/`chart_parse` |
| `OCR_TOOL_NGL` | `99` | GPU offload 层数；`0`=强制纯 CPU。GPU 起不来会自动回退 CPU |
| `OCR_TOOL_LLAMA_VARIANT` | `vulkan` | `make install-llama` 下载的后端：`vulkan`(核显) / `cpu` |

## 打包 / 独立安装

参照 `py-muti-scrcpy` 的做法，用 **PyInstaller** 把应用（含 Python 运行时 + PySide6）打成
自带运行时的独立目录，**不依赖本源码目录**：

```bash
make pkg-binary     # → dist/ocr-tool/ 独立运行目录（含 ocr-tool 可执行）
make install-app    # 打包并安装到 ~/.local/lib/ocr-tool + 注册桌面入口
make pkg            # 生成 .deb + .rpm 到 dist/pkg/（需 nfpm）
make pkg-clean      # 清理 dist/ build/ 与 _version.py
```

- 模型与 `llama-server` **不打进包**（约 1.3GB，过大），仍在共享数据目录。
  独立安装版首次使用先下载后端：`ocr-tool --setup-backend`（若已 `make install-llama` 过则直接共用）。
- 打包前会 `make generate-version-file` 把版本冻结进 `ocr_tool/_version.py`，
  让无 `pyproject.toml` 的产物也能正确显示版本。

## 日志

- 应用日志：`$XDG_RUNTIME_DIR/ocr-tool.log`（或 `/tmp/ocr-tool-<uid>/`）
- 推理后端日志：`llama-server.log`（同目录）
- 后台拉起日志：`ocr-tool-spawn.log`（同目录）

## Make 命令

| 命令 | 说明 |
|------|------|
| `make` / `make help` | 列出命令 |
| `make install` | 开发用：Python 依赖 + editable + 桌面入口 |
| `make install-llama` | 下载预编译 `llama-server`(默认 Vulkan) + 官方 GGUF 到共享目录（免编译） |
| `make install-app` | 独立安装：PyInstaller 打包并装到 `~/.local`（不依赖源码） |
| `make pkg-binary` / `make pkg` | 打独立目录 / 生成 deb+rpm（需 nfpm） |
| `make pkg-clean` | 清理打包产物 |
| `make serve` | 手动前台启动推理服务（调试） |
| `make install-desktop` | 仅装桌面入口（不跑 pip） |
| `make run` | 截图 OCR（托盘单例） |
| `make quit` / `make status` | 管理托盘应用 |
| `make show-version` | 打印版本（读自 `pyproject.toml`） |
| `make set-version VERSION=x.y.z` | 改版本；若在 git 仓库则自动 commit |

版本号唯一权威源：`pyproject.toml` 的 `version` 字段。
