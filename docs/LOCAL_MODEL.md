# 本机 AI 建议稿运行时

稿件改写可接入本机的 OpenAI-compatible `/v1/chat/completions` 接口。本文记录已验证的一组 CPU 配置，使用独立的 `llama.cpp` 服务和官方 `Qwen3-0.6B-Q8_0` 权重，监听 `127.0.0.1:11435`。参考录音与声音合成由 ZipVoice 单独处理。

小模型的建议需要逐句对照。数字和缩写检查只能发现部分错误，不能保证计划、待确认、否定等业务语义保持一致。建议不应自动替换原稿。

## 本机资源与来源

将模型、工具与运行日志放在源码和同步目录以外，以实际安装路径填写本机配置。

| 项目 | 锁定值 |
| --- | --- |
| 模型 | [Qwen/Qwen3-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF)，`Qwen3-0.6B-Q8_0.gguf` |
| 模型 revision | `23749fefcc72300e3a2ad315e1317431b06b590a` |
| 模型大小 | 639,446,688 bytes |
| 模型 SHA-256 | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |
| 模型许可 | Apache-2.0 |
| 工具 | [llama.cpp b12138 官方 Windows CPU x64 包](https://github.com/ggml-org/llama.cpp/releases/tag/b12138) |
| 工具版本 | `0.9.0-dev`，build 12138，commit `f7b41cca4fecc8164c7ea45bfb55dfff7633842f` |
| 工具 ZIP 大小 | 24,728,204 bytes |
| 工具 ZIP SHA-256 | `aa345c7052ef2d03814a3d5a13bd55e70bc27057ee3021d8371199f6ae67e355` |
| 工具许可 | MIT |

历史验证核对了两个 SHA-256。模型和工具总下载约 664 MB，含解压工具和验证记录的运行目录约 696 MiB。

## 运行约定

`config/local.json` 中的 `rewrite` 可按以下示例连接已启动的服务：

```json
{
  "enabled": true,
  "base_url": "http://127.0.0.1:11435/v1",
  "model": "voice-studio-qwen3-0.6b"
}
```

启动参数采用 CPU 4 线程、单槽、2048 context、`--batch-size 128 --ubatch-size 64 --cache-ram 0 --poll 0 --offline --no-webui --jinja --reasoning off --reasoning-budget 0 --sleep-idle-seconds 90`。没有 GPU 依赖。单次输入应限制在 600 字以内，输出限制为 800 tokens；超长内容需要先分段。

关闭 thinking 的验证组合为服务端 `--reasoning off --reasoning-budget 0`，请求同时带 `chat_template_kwargs: {"enable_thinking": false}` 和 `reasoning_effort: "none"`。切换 llama.cpp 版本时可用 `llama-server --help` 核对参数。

模型空闲 90 秒后自动卸载权重和 KV cache，新请求会重新加载。历史测试观察到 `/props` 返回 `is_sleeping: true`，工作集由约 898 MiB 降至约 22 MiB，private memory 约 11 MiB。健康检查和状态查询可在休眠时使用。[llama.cpp 空闲休眠说明](https://github.com/ggml-org/llama.cpp/blob/f7b41cca4fecc8164c7ea45bfb55dfff7633842f/tools/server/README.md#sleeping-on-idle)

工作台启动脚本负责管理本项目的 helper。停止时必须同时核对记录的 PID、可执行路径和创建时间；不能按端口或进程名称批量停止其他模型服务。模型文件不应随工程 ZIP、源码或普通业务导出复制。

受管入口为 `scripts/local_model.py start|stop`，由 `scripts/start.ps1` 与 `stop.ps1` 调用。
启停使用本机配置的 `rewrite.managed`（executable、model_path、runtime_dir、port、threads、context_size、sleep_idle_seconds）。
启用受管模式后，启动和停止声稿的入口会一并处理这个服务，状态文件为配置中 `runtime_dir` 下的 `managed-process.json`。

## 已做的短测与局限

2026-11-05 的 CPU 短稿测试中，模型加载后的工作集约 898 MiB，private memory 约 337 MiB。以下结果来自专门编写的合成示例。

- WMS/12/3 样本耗时 3.153 秒，返回内容与原文一致，没有思考块；它证明真实请求成功，不证明改写水平。
- DHL/20/8/12 样本耗时 1.876 秒，模型把 `2026-11-05` 改为中文日期格式。逐字符日期保护应拒绝这一建议。
- 加强 token 保留要求后，补测耗时 1.556 秒且保留全部 token，但将“计划处理”压缩为“处理”，又将“不代表已经完成”改为“计划未完成”，存在语义改动。这说明通过数字检查仍需人工对照。

涉及日期、数量和业务状态的稿件需逐句核对，新文案的配音另行试听。

## 下载故障记录

历史验证中，完整下载曾停滞，同一官方 revision 的 Range 请求成功。4 路 16 MiB 分段下载用时 150.41 秒，逐段核对 `Content-Range`，合并后的 SHA-256 与官方值一致。日常启动只读取已安装的权重。
