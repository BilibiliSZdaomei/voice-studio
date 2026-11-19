# 声稿 / Voice Studio

声稿是一套在本机运行的中文配音工作台。把文案拆成片段，用自己的声线逐段试听和重做，最后导出音频、字幕或由原创小机器人讲解的视频。当前版本为 **0.3.1**。

## 在工作电脑上打开

1. 登录可访问此仓库的 GitHub 账号，点击 **Code → Download ZIP** 并解压，或使用下方的 Git 命令。
2. 确认 Windows 上的 `python --version` 为 **3.12 或更高版本**。打开源码目录，双击 **启动声稿.cmd**。浏览器会打开 [本地工作台](http://127.0.0.1:8787)。
3. 选择 **新建项目**，套用一个起稿模板，保存后切到逐段视图，就能体验编辑流程。首次打开时项目列表为空；同一台电脑下次启动时可以继续编辑。
4. 用完双击 **停止声稿.cmd**。以后通过 Git 克隆的副本可在该目录运行 `git pull --ff-only` 更新源码。

```powershell
git clone https://github.com/BilibiliSZdaomei/voice-studio.git
cd voice-studio
.\启动声稿.cmd
```

**看界面、试写文案只需 Python。** API 使用标准库，前端无需构建。个人声线和已有配音工程保存在制作电脑上，GitHub 只分发源码、模板、测试和文档。

## 按需启用制作功能

| 想做什么 | 需要准备什么 |
| --- | --- |
| 编辑、分段、整理 Markdown、管理词典 | Python 3.12+ |
| 用自己的声线生成、局部重做 | 独立 ZipVoice 推理环境，模型、词典、声码器，以及参考 WAV 和逐字对应的文本 |
| 试听已完成的段落或接着制作 | 从制作电脑单独导出的工程 ZIP，在工作台中选择“恢复工程” |
| 导出音频与段级字幕 | 已生成的段落。WAV、SRT 和工程 ZIP 可直接导出，MP3 另需 ffmpeg |
| 使用本地 AI 改写短稿 | 配置本机模型服务，核对建议与原稿后再采用。规则整理可独立使用 |
| 生成讲解视频 | 已导出的完整 WAV，另需配置 HyperFrames、Node、浏览器、字体、ffmpeg 和 ffprobe |

要启用模型，可将 [配置示例](config/local.example.json) 复制为同目录的 `local.json`，填写这台电脑实际使用的路径，再重启工作台。纯编辑可跳过配置。语音依赖的已验证版本见 [推理依赖](requirements-inference.txt)，改写与视频各自的配置见 [本地模型](docs/LOCAL_MODEL.md) 和 [视频导出](docs/VIDEO.md)。

## 日常制作

- 文案可直接粘贴，也能从 UTF-8 TXT 或 Markdown 导入。保存后按句分段，试听后只需重做有问题的段落。新稿件的发音、语气和停顿仍需试听确认。
- 发音词典单独保存朗读用词，字幕沿用原稿。更新词典后可预览差异，再选择是否用于当前项目。批量导入一次最多 20 篇，可排队生成。
- 配音完成后可以导出整稿。工程 ZIP 含文案与已生成配音，适合单独传给需要继续制作的人。恢复后生成新的项目，重新合成时使用接收电脑配置的声线。
- 视频支持横竖屏，可选纯字幕或原创虚拟讲解员。机器人嘴部随配音音量开合，角色和动作可在导出的工程中编辑。制作过程无需人物照片或拍摄素材。

## 数据与开发

Windows 首次启动默认把数据写到 `%LOCALAPPDATA%\VoiceStudio\data`。设置了 `CODEX_ARTIFACT_ROOT` 时默认使用其下的 `VoiceStudio\data`。本机配置中显式指定的绝对 `data_root` 优先。项目文案、录音、导出和日志均保存在仓库外；源码更新后仍使用原数据目录。服务仅监听本机回环地址。

| 目录 | 内容 |
| --- | --- |
| `apps/web/` | 浏览器界面与可复用的生产工具 |
| `apps/api/voice_studio/` | Python API、队列、工程恢复、语音和视频处理 |
| `scripts/` | 启停、诊断、命令行客户端与测试入口 |
| `tests/` | 可用合成数据运行的回归测试 |
| `docs/` | [架构](docs/ARCHITECTURE.md)、[API 与 CLI](docs/API.md)、[产品与路线](docs/ROADMAP.md)、[验证记录](docs/VALIDATION.md) |

```powershell
# 查看当前电脑可用的资源
python -B -X utf8 scripts/run_server.py --diagnose

# 前台启动，可在同一窗口查看服务输出
python -B -X utf8 scripts/run_server.py --port 8787

# 从另一个终端查看本地项目
python -B -X utf8 scripts/voice_cli.py projects

# 运行自动检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```
