# 本地虚拟讲解与字幕视频

视频导出消费已经完成的 WAV 和该导出目录中的 `manifest.json` 段级时间轴，不会再次生成配音。模板采用声稿的暖纸色、深墨色和梅紫色，支持 1920×1080（16:9）与 1080×1920（9:16），使用本机 HyperFrames CLI 渲染 MP4，保留本地可编辑 HTML 工程。

0.3 新增 `virtual_host` 虚拟讲解员，首个角色为本地原创卡通小机器人 `studio-bot`；纯字幕 `captions` 继续可选。角色用可编辑 SVG 呈现，复用本人声音，以音频能量驱动嘴部开合，配合眨眼和轻微动作。无需照片或短视频，不仿本人的脸；真人素材驱动路线已取消。嘴部动作不宣称精细音素口型，实时对话和字级强制对齐仍未验收。

本文件的 0.3 内容描述新增工程约定。既有实测段落仅证明 0.2 字幕视频，虚拟讲解员的实际渲染及工程验证以本轮新增记录为准。

## 本机资源配置

在不进入 Git 的 `config/local.json` 中增加 `video` 对象。所有路径需为已安装资源的绝对路径；运行导出不会执行 npm 安装、下载模型或登录云服务。

```json
{
  "video": {
    "node": "C:/VoiceTools/node/node.exe",
    "hyperframes_cli": "C:/VoiceTools/node_modules/hyperframes/dist/cli.js",
    "browser": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "font": "C:\\Windows\\Fonts\\msyh.ttc"
  }
}
```

以上为路径示例。历史实测版本为 HyperFrames 0.9.17 和 Node 24.15.0。`gsap` 默认从 CLI 同目录的 `vendor/npm/gsap@*/dist/gsap.min.js` 查找，也可显式配置。`ffmpeg` 与 `ffprobe` 默认从 PATH 查找，可显式配置各自绝对路径。`availability()` 检查文件是否已配置，`render()` 运行 HyperFrames check、渲染与 ffprobe 核验。

如需迁移，先在代码目录及同步根之外安装同版本 HyperFrames，再更新本机配置；不可把 `_npx` 缓存地址当作跨机器可移植路径。工程 ZIP 内不包含 Node、Chrome、HyperFrames CLI 或大型模型。

## 模块接口

```python
from voice_studio.video import VideoExporter, VideoCancelled

exporter = VideoExporter(config, data_root)
availability = exporter.availability()
result = exporter.render(
    project_snapshot,
    export_snapshot,
    {"aspect_ratio": "16:9", "fps": 30, "layout": "virtual_host", "avatar_id": "studio-bot"},
    on_progress=lambda stage: record_stage(stage),
    cancel_event=threading.Event(),
)
```

`render` 必须由后台任务调用。阶段为 `preparing`、`checking`、`rendering`、`packaging`；不提供无依据的百分比或剩余时间。取消抛出 `VideoCancelled`，终止实际渲染进程及其子进程；不会登记半成品，也不会改变此前音频。缺少本机资源返回 `AppError(503, ...)`；输入导出或时间轴失效为 409；渲染与验证失败为 500，工程和日志保留。

返回值包括 `video_url`、`composition_url`、`bundle_url`、`manifest_url`、`duration_seconds`、`video_duration_seconds`、`aspect_ratio`、`source_revision`、`renderer`、`renderer_version` 和 `subtitle_alignment`。0.3 增加模式与角色信息，虚拟讲解员结果中的 `animation` 为 `audio_energy_driven`，元数据中的 `mouth_sync` 为 `amplitude_not_phonemes`。`source_revision` 为音频导出绑定的源版本；服务在最终登记前仍应核对项目是否在任务期间改变。

输入 WAV 只允许当前数据根的 `exports/<project-id>/` 内已有文件，禁止网络 URL 和路径穿越。模块核对 PCM 实际时长、单调时间轴、段序/文案、音频版本、生成设置和原导出快照。改稿、重做音频或调整拼接设置后必须重新导出音频。

## 模式与角色（0.3）

| 选项 | 约定 |
| --- | --- |
| `layout="virtual_host"` | 角色与字幕共同讲解；界面默认选择。 |
| `layout="captions"` | 保留原字幕讲解模式。API 省略 `layout` 时也使用该模式，兼容旧客户端。 |
| `avatar_id="studio-bot"` | 首个原创机器人角色。角色标识与 SVG 资源独立于声线，保留后续替换空间。 |

`avatar.py` 的 `audio_envelope(wav_path, sample_hz=15)` 从本次 mono PCM16 源 WAV 计算 RMS 能量，经百分位归一化后返回 `{sample_hz, duration_seconds, levels}`，供 SVG 嘴部开合使用。`build_host_composition(duration, aspect, envelope, avatar_id="studio-bot")` 使用 `templates/virtual-host.html` 生成 `compositions/host.html`，子工程标识为 `voice-host`。

眨眼与身体轻动使用暂停的有限 GSAP 时间轴，随工程时间确定，渲染跳转后仍应得到相同姿态；静音期间停止说话动作。该方案不识别每个字的发音，也不估计情绪。完整原配音及真实段间静音保持不变。

角色 SVG、动作参数与动画代码内联于 `compositions/host.html`，能量采样另存为 `audio-envelope.json`，均随工程保存并可在本地继续编辑；没有重复的独立 SVG 源文件。替换角色需保持工程使用的动作节点或同时修改动画绑定。新角色标识须在代码中注册并校验，界面只展示实际支持的角色。无需导入本人形象素材，也不自动连接云端素材或发布服务。

## 输出与时长

每次输出到 `<data_root>/videos/<project-id>/<new-uuid>/`，不会覆盖之前的视频。输出包括：

- `video.mp4`：HyperFrames 实际渲染的 H.264/AAC 视频。
- `index.html`、模式对应的 `compositions/` 子工程、`hyperframes.json`、`BRIEF.md`：可复用的 HyperFrames 工程。字幕模式保留 `compositions/captions.html` 和逐卡节点供 Studio 编辑；虚拟讲解模式使用 `compositions/host.html`，保留可编辑 SVG、能量数据与动画代码。
- `assets/`：本次导出使用的 WAV、本地 GSAP 与中文字幕字体，工程不依赖 CDN。
- `audio-envelope.json`：虚拟讲解模式使用的音频能量采样；与内联 SVG 和动作参数一同进入工程 ZIP。
- `manifest.json`：源 WAV SHA-256、段级时间轴、音频源版本、比例、帧率和渲染器版本；0.3 另记录布局与角色信息。
- `hyperframes-project.zip`：HTML 工程与所需素材，便于归档和继续编辑。
- `check.log`、`render.log`、`ffprobe.json`：实际验证证据。

长句按标点和卡片长度拆分显示，每张卡的显示时间按原段落字符占比分配。它是**段级时间轴上的估算字幕卡**，并非字级或词级强制对齐。视频使用完整原 WAV，段间静音保持原样，字幕在静音期间自然留空。MP4 因帧边界和 AAC 封装可能比 PCM 长约一帧；校验容差为 0.12 秒，超过则导出失败。

运行过程的临时目录和 HyperFrames 缓存置于本次 UUID 输出目录；只对子进程设置环境变量，不修改系统环境。模型与参考录音保持原位置。自动化检查、MP4 可播放与音频流存在，不代表新文案的声音质量已经由用户试听验收。

## 0.2 字幕视频实测（2026-11-05）

9 项模块测试通过，覆盖失效导出、音频版本、时长、路径穿越、输入设置、HTML 转义、原生字幕轨道和真实子进程取消。使用之前完成的 11.85825 秒短样音进行了实际 HyperFrames 渲染：1920×1080 / 30 fps / H.264 + AAC，MP4 时长 11.866667 秒。最终工程的 lint、runtime、layout、motion、contrast、craft、audio 均为零错误、零警告；已检查解码后的 1 秒与 9 秒画面，中文清晰且未溢出，工程 ZIP 完整性通过。

9:16 另用 80 字标题、中英混排长句做了标准子工程 check 和 1/5/9 秒截图检查，未见布局错误或文字溢出；因一次刻意放入 8 张字幕卡，CLI 提示单轨道较密集的编辑性警告，保留单字幕轨道。此排版 fixture 与复用音频内容不对应，没有作为成品 MP4 交付。尚未对几分钟长视频做完整渲染验收。

随后通过实际生产队列，将既有377字/12段的连续讲解导出为9:16成品：1080×1920、30fps、H.264/AAC、62.733333秒，较源WAV仅长0.004625秒。
实际渲染约2分45.6秒，解码1/31/60秒画面均无乱码或溢出；ZIP完整，HTTP Range206。
稿件、声音快照、设置、音频版本和WAV SHA256前后相同，仅新增视频并更新项目revision。
该长竖屏检查无错误，保留一条“同字幕轨道15张卡”的编辑性警告。

## 0.3 虚拟讲解员验证范围

新增能力需单独检查横屏与竖屏实际渲染、说话与静音时嘴部状态、眨眼和轻动、字幕布局、原音频及版本保持，以及角色 SVG、动作数据和工程 ZIP 的完整性。新结果不能用上面的字幕视频检查替代；具体通过项、成品与证据路径见下方本轮虚拟角色验证。角色外观与动作观感仍需用户观看确认，新文案声音仍需用户试听。

## 0.3 虚拟讲解员实测

本轮通过真实后台任务分别完成16:9与9:16原创机器人短片，源WAV11.85825秒，MP4均11.866667秒；原稿、设置和既有音频版本不变。SVG、音量时间轴、工程ZIP均可读取。实际解码的1.5秒和6秒画面已检查，无乱码、字幕与角色无溢出。静音/语音/静音及往返seek验证来自独立角色预览，确定性状态一致。

85项自动检查、9项前端合同检查、6项真实页面检查通过；完整API提交和渲染另有真实任务记录。初次竖屏检查进程曾退出且日志为空，相同工程独立check正常；保留失败记录并增加退出码诊断后，受控重试完成，首次退出原因未确认。

形象与动作观感需由使用者观看确认，新文案的音色与读法需另行试听。
