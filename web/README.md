# LoveChoice Voice Console

这是一个前端 + 后端控制台：

```text
Browser UI
  -> start / stop ASR, LLM, TTS services
  -> Browser mic -> WebSocket -> Silero VAD
  -> Qwen3-ASR -> llama.cpp -> CosyVoice3 -> Browser audio
```

页面分成三个入口：

- 对话：`/static/index.html`，只放实时对话、麦克风、波形和延迟指标。
- 服务：`/static/services.html`，放 ASR/LLM/TTS 状态、一键启动/停止、单服务启动/停止和日志。
- 配置：`/static/settings.html`，放 ASR/LLM/TTS 路由、生成参数、VAD 参数、服务启动命令。

## 1. 只需要先启动 Web 控制台

模型服务不用手动提前开，进入服务页面后点“一键启动”即可。

```bash
conda activate qwen3-asr

pip install -r /mnt/c/Users/Me/Documents/Codex/2026-06-04/cosyvoice3-llama-cpp-qwen3-5-9b/outputs/voice_web_app/requirements.txt

python /mnt/c/Users/Me/Documents/Codex/2026-06-04/cosyvoice3-llama-cpp-qwen3-5-9b/outputs/voice_web_app/web_server.py \
  --host 0.0.0.0 \
  --port 7860
```

打开：

```text
http://127.0.0.1:7860
```

服务页面：

```text
http://127.0.0.1:7860/static/services.html
```

## 2. 一键启动做了什么

页面左上角“一键启动”会依次启动：

```text
ASR: qwen-asr-serve /home/me/project/Qwen3-ASR-1.7B
LLM: ~/project/llama.cpp/build-cuda/bin/llama-server
TTS: trained_tts_server.py --load_vllm --fp16
```

一键启动是串行启动，不是同时启动。默认等待：

```text
ASR 启动后等待 25 秒
LLM 启动后等待 10 秒
然后启动 TTS
```

这样做是为了避免多个 GPU/vLLM 服务同时初始化时显存采样互相干扰。你日志里的这个错误：

```text
AssertionError: Error in memory profiling...
```

就是 TTS 内部 vLLM 初始化时，别的进程释放/占用显存导致采样前后不一致。

默认命令在这里：

```text
service_profiles.example.json
```

如果路径和你的机器不完全一致，复制一份：

```bash
cp /mnt/c/Users/Me/Documents/Codex/2026-06-04/cosyvoice3-llama-cpp-qwen3-5-9b/outputs/voice_web_app/service_profiles.example.json \
   /home/me/project/voice_services.local.json
```

修改 `/home/me/project/voice_services.local.json` 后启动：

```bash
python /mnt/c/Users/Me/Documents/Codex/2026-06-04/cosyvoice3-llama-cpp-qwen3-5-9b/outputs/voice_web_app/web_server.py \
  --host 0.0.0.0 \
  --port 7860 \
  --service-config /home/me/project/voice_services.local.json
```

也可以直接打开配置中心：

```text
http://127.0.0.1:7860/static/settings.html
```

在配置中心里改 Working Directory、Health URL、Start Command，然后点“应用配置”。

## 3. 默认服务参数

ASR：

```bash
conda run -n qwen3-asr qwen-asr-serve /home/me/project/Qwen3-ASR-1.7B \
  --served-model-name qwen3-asr \
  --gpu-memory-utilization 0.45 \
  --host 0.0.0.0 \
  --port 8001
```

LLM：

```bash
./build-cuda/bin/llama-server \
  -m ./Qwen3.5-9B.Q8_0.gguf \
  --alias qwen3.5-9b \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 99 \
  -c 4096 \
  --jinja \
  --reasoning off
```

TTS：

```bash
conda run -n cosyvoice_vllm python /mnt/c/Users/Me/Documents/Codex/2026-06-04/cosyvoice3-llama-cpp-qwen3-5-9b/outputs/cosyvoice_tts_api/trained_tts_server.py \
  --repo_dir /home/me/project/CosyVoice \
  --model_dir /home/me/project/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B \
  --speaker hanser \
  --load_vllm \
  --fp16 \
  --host 0.0.0.0 \
  --port 50000
```

## 4. 日志位置

Web 控制台启动模型后，日志会写到：

```text
outputs/voice_web_app/runtime/logs/asr.log
outputs/voice_web_app/runtime/logs/llm.log
outputs/voice_web_app/runtime/logs/tts.log
```

页面左侧每个服务点“日志”也能直接查看。

## 5. 调参建议

更快结束收音：

```text
VAD Silence ms: 250 - 320
```

更不容易误触发：

```text
VAD Threshold: 0.55 - 0.65
Min Utterance ms: 350 - 500
```

降低显存压力：

```text
ASR gpu-memory-utilization: 0.35 - 0.45
LLM context: -c 4096
TTS: --load_vllm --fp16
```
