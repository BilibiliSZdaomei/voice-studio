# 本地 API

默认地址 `http://127.0.0.1:8787`。不开放跨域访问。客户端先读取
`GET /api/status` 的 `csrf_token`，写操作用请求头 `X-Studio-Token` 携带。
请求体为 JSON 对象，错误响应为 `{"error":"具体原因"}`。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/status` | 引擎、声线、活动任务及本机路径 |
| `GET /api/voice/sample` | 本机配置的独立样音，支持 HTTP Range。没有样音时返回 404，状态接口中的 `voice.sample_url` 为空 |
| `GET /api/projects` | 项目列表，返回 `projects` 数组 |
| `POST /api/projects` | 使用 `title`、`script` 创建并分段 |
| `GET /api/projects/{id}` | 当前项目、版本及段落状态 |
| `PUT /api/projects/{id}` | 更新 `title`、`script`、`settings`，携带 `expected_revision` |
| `PATCH /api/projects/{id}/segments/{sid}` | 更新 `text`，携带项目的 `expected_revision` |
| `POST /api/projects/{id}/generate` | 异步生成未完成段落 |
| `POST /api/projects/{id}/segments/{sid}/generate` | 异步重新生成指定段落 |
| `GET /api/jobs/{id}` | 获取任务状态与已完成段数 |
| `POST /api/jobs/{id}/cancel` | 请求取消任务 |
| `POST /api/projects/{id}/export` | 导出当前完整配音，返回 WAV、可用时 MP3、段级 SRT、工程 ZIP 地址 |
| `GET /files/{relative-path}` | 读取数据根内允许的成品文件，禁止越界 |
| `POST /api/shutdown` | 本机启动脚本使用的安全退出接口 |

项目设置使用 `speed` 和 `pause_seconds`。修改文本或语速后应生成新的配音；
只修改句间停顿时可以直接重新拼接导出。编辑遇到 `409` 时应重新载入项目，
不可静默覆盖其他页面的修改。任务未结束时等待或取消后再编辑。

`/api/status` 中的 `engine.model_available` 表示推理环境与模型文件就绪，`engine.available` 还要求本机默认参考录音和转写可用。单篇及批量入队会逐个检查所选声线。首次生成时，从未绑定参考录音且没有生成记录的默认声线草稿会绑定当前本机默认档案。

生成任务状态为 `queued / running / completed / failed / cancelled`。
长任务不占用 HTTP 请求；建议每 1–2 秒轮询，依据 `completed/total` 显示进度。
生成成功表示音频文件和流程校验通过，不代表用户已验收声线。

## 命令行复用

服务启动后可调用，无额外依赖：

```powershell
python -B -X utf8 scripts/voice_cli.py projects
python -B -X utf8 scripts/voice_cli.py narrate D:\文案.txt --title "项目介绍" --wait --export
python -B -X utf8 scripts/voice_cli.py generate <project_id> --wait
python -B -X utf8 scripts/voice_cli.py export <project_id>
```

导出地址仍由本机服务提供。工程 ZIP 可以通过工作台“恢复工程”或 CLI `restore` 导入为新项目；
恢复时保留词典快照、启用状态与对应读法，并给项目和音频分配新标识。工程包保存文案和成品，个人声线的参考录音、转写与本机路径留在制作电脑。接收电脑使用本机默认声线继续合成。

## 0.2 生产接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/jobs` | 当前队列与近期任务；`kind` 区分 `speech` / `video` |
| `POST /api/batch` | `projects` 数组及 `generate` 布尔值；1–20 篇、总计最多 180000 字 |
| `GET /api/voices` | 本机声线档案 |
| `POST /api/voices` | 注册或带 `id`、`expected_revision` 更新档案；默认档案只读 |
| `GET /api/voices/{id}/sample` | 档案样音，仅引用本机 WAV |
| `GET /api/dictionary` | 词典 revision 及 entries |
| `PUT /api/dictionary` | 带 expected_revision 保存全局词典 |
| `POST /api/projects/{id}/dictionary/preview` | 查看当前词典会如何改变各段读法，不修改项目 |
| `POST /api/projects/{id}/dictionary/apply` | 带 expected_revision、enabled，冻结当前词典并使实际变化的段落失效 |
| `POST /api/projects/import` | 原始 `application/zip` 请求体，最多 64MB，返回恢复后的新项目 |
| `POST /api/projects/{id}/prepare` | 带 mode、expected_revision 生成 cleanup 或 rewrite 建议 |
| `POST /api/projects/{id}/proposals/{proposal}/apply` | expected_revision、confirmed:true，确认后采用 |
| `POST /api/projects/{id}/proposals/{proposal}/undo` | 采用后无后续修改时，确认撤销到原稿快照 |
| `POST /api/projects/{id}/video` | expected_revision、aspect_ratio，创建本地视频任务 |

不同项目可以连续排队，同一项目已有任务时不能再编辑或重复提交。取消尚未启动的任务不会取消
其他项目；已排队任务与输入快照持久保存。服务器关闭会取消当前工作，待排队任务留待重启。

项目的 `voice_snapshot` 和 `dictionary_snapshot` 冻结实际生产设置；更改全局档案不自动改变旧配音。
段落同时保留原 `text` 和 `spoken_text`。声线变化会重置全稿，词典只重置读法真正变化的片段。

稿件建议保留 original_text/proposed_text/source_revision/warnings。确认采用仍需通过原稿版本、
数字及缩写检查；有后续修改后撤销会返回409，防止覆盖新工作。模型接口只允许本机 HTTP，不跟随重定向。

视频任务完成后 `result` 及 `project.video` 返回 MP4、完整工程 ZIP、单独 HTML 和 manifest 地址。
HTML 以附件下载，工作台首页正常显示。字幕卡为段级估算，非字级强制对齐；具体接口见 [视频模块](VIDEO.md)。

## 0.3 虚拟讲解员

视频请求增加 `layout: "virtual_host"` 与 `avatar_id: "studio-bot"`，使用原创机器人，不接收真人照片或拍摄素材。
`layout: "captions"` 保留纯字幕；为兼容旧 API 客户端，未传 `layout` 时仍按纯字幕处理。
新工作台界面和 CLI 默认选择虚拟讲解员，CLI 可用 `--layout captions` 切换。

结果与 manifest 记录 `layout`、`avatar_id`、`animation: "audio_energy_driven"`。
`mouth_sync: "amplitude_not_phonemes"` 说明嘴部由实际 WAV 音量驱动，不代表精确音素对齐。
工程内 `compositions/host.html` 保存可编辑 SVG 及确定性动作，`audio-envelope.json` 保存音量时间轴。
旧结果缺少 `layout` 时按纯字幕显示，不能因当前界面选择而误标历史视频。
