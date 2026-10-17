# F1 Harness V1

本机 F1 视频制作 Harness 的源码与文本资料快照。涵盖资料核验、文案、配音、素材与剪辑、合成、审核和交付；保留计划书、提示词及里程碑历史，作为继续迭代和探索其他工作流的起点。

项目内原有名称 `F1 Hermes V1`、Python 项目名 `f1-hermes` 和模块名 `hermes` 保留。GitHub 中的目录名称为 **F1 Harness V1**。

## 收录内容

| 路径 | 内容 |
| --- | --- |
| `hermes/` | 全部程序代码，含 `hermes/data/` 下运行需要的静态样例、标签原型与读音表。 |
| `tests/` | 自动测试和实机检查脚本。 |
| `web/` | 网页界面与静态样稿。 |
| `launcher/` | macOS 启动器源码、构建脚本与必要图标。 |
| `skills/` | 技能文件及随附的第三方许可证。 |
| `tools/*.swift` | 界面操作、OCR 和网页截图工具的 Swift 源码。 |
| `docs/` | 外置硬盘文档中的文本文件，包括计划书、运行必需的 `提示词/`、工程说明、里程碑、案例分析及文本代码备份。 |
| `CLAUDE.md` | 原项目的交接与维护记录。 |
| `pyproject.toml`、`uv.lock` | Python 依赖定义与锁定版本。 |

原 `docs/` 同时包含截图、试听音频、视频等约 326 MB 的二进制附件；这些附件未上传，历史文档中指向它们的相对链接需要配合外置硬盘或归档使用。所有可解码为 UTF-8 且不含空字节的文档文件均保留，macOS 元数据和缓存除外。

原 `tools/websnap` 是已编译的可执行文件，并非子目录；仓库只保留其 `websnap.swift` 源码。

## 本地路径与恢复

这是现有本机项目的源码快照。默认路径仍保留原配置：代码盘 `/Volumes/HermesCore`，文档与每期文件位于外置硬盘的 `F1 Hermes V1` 目录。上传时未修改正在运行的项目。

在新检出目录准备 Python 环境时，进入本目录执行：

```bash
uv sync --frozen
```

服务支持通过环境变量指定代码与项目根目录。若将本检出目录同时作为两者，可在本目录执行：

```bash
export HERMES_CORE="$PWD"
export HERMES_ROOT="$PWD"
uv run --frozen python -m hermes.server
```

网页地址为 `http://127.0.0.1:8790/`。上述设置使程序读取本仓库的 `docs/提示词/`，运行数据和输出会写入本目录内已忽略的路径。若将 `HERMES_ROOT` 指向另一个目录，那里也必须提供 `docs/提示词/`。

完整制作仍需按现有工程说明准备 Codex CLI 与本机登录、剪映、FFmpeg、yt-dlp、rclone、字体、品牌素材及模型；这些环境、授权和媒体不随源码分发。默认模型配置与本机工具路径见 `hermes/config.py`；换机时应核对实际可用配置。

Swift 工具可在 macOS 安装开发工具后于本目录构建：

```bash
mkdir -p tools/bin
swiftc -O -o tools/bin/hermes-ui tools/macui.swift
swiftc -O -o tools/bin/hermes-ocr tools/ocr.swift
swiftc -O -o tools/websnap tools/websnap.swift
```

`launcher/main.c` 仍使用原机器的磁盘映像与挂载点，换机使用桌面启动器前需要适配路径；`launcher/build.sh` 会重建桌面上的 `F1 Harness.app`。

## 测试

```bash
uv run --frozen python -m pytest -q
```

`tests/conftest.py` 将测试数据路径切换到临时目录并关闭定时任务。`live_*.py` 是实机或联网检查，部分需要本机媒体、授权或会消耗模型额度，应按具体脚本的说明单独运行。

## 排除范围

`data/`（含数据库与 `secrets/`）、`.venv/`、`cache/`、`tools/models/`、`tools/bin/`、`logs/`、Python 与 pytest 缓存、macOS `._*` 文件、`productions/`、`素材库/`、`assets/`、`jianying/` 及磁盘映像均不上传。大文件继续使用既有 Google Drive 归档。

根目录 `data/` 与代码内的 `hermes/data/` 用途不同：前者被忽略，后者是程序必需的静态文件，纳入版本控制。

技能的第三方许可见 `skills/HERMES-AGENT-LICENSE.txt` 及各技能随附的许可证；这些许可仅按原有覆盖范围保留。
