# 枝语 BranchWhisper

枝语 BranchWhisper 是一个本地语音 AI 对话控制台，核心链路是：

```text
浏览器麦克风 -> WebSocket -> VAD -> ASR -> LLM -> TTS -> 浏览器播放
```

项目同时提供服务编排、配置中心、日志查看、记忆管理和接入管理页面，方便在本机启动 ASR、LLM、TTS 以及微信个人号等外部接入。

## 快速启动

```bash
python web/web_server.py --host 127.0.0.1 --port 7860
```

打开：

```text
http://127.0.0.1:7860
```

常用页面：

- 对话页：`http://127.0.0.1:7860`
- 服务页：`http://127.0.0.1:7860#services`
- 配置页：`http://127.0.0.1:7860#settings`
- 接入页：`http://127.0.0.1:7860#integrations`

## 服务配置保存

在配置页修改 ASR、LLM、TTS 的启动命令、工作目录、健康检查地址后，点击应用配置即可保存。

默认保存位置：

```text
web/runtime/service_profiles.json
```

如果需要使用自定义配置文件，可以启动时传入：

```bash
python web/web_server.py --service-config /path/to/service_profiles.json
```

## 微信个人号接入环境

微信个人号接入基于 OpenClaw 和 `@tencent-weixin/openclaw-weixin`。枝语只负责检测、启动和桥接，不直接保存微信 token。

先检查本机环境：

```bash
node -v
npm -v
ffmpeg -version
```

安装 OpenClaw CLI 和微信适配器：

```bash
npm install -g openclaw
npm install -g @tencent-weixin/openclaw-weixin-cli
openclaw --version
```

如果是在 AutoDL、容器或自定义 Node 目录里安装，需要确保启动枝语的同一个 shell 里能找到 `node/npm/npx/openclaw`。例如：

```bash
export PATH=/root/autodl-tmp/tools/node/bin:$PATH
node -v
npm -v
npx --version
openclaw --version
```

然后进入接入页添加微信个人号实例，按页面提示扫码登录并启动桥接。

## 运行数据

运行时数据默认放在 `web/runtime/`：

- `settings.json`：主配置
- `service_profiles.json`：服务启动配置
- `integrations.json`：接入实例配置
- `memory.sqlite3`：记忆数据
- `logs/`：服务日志

这些运行时文件通常不需要提交到 Git。
