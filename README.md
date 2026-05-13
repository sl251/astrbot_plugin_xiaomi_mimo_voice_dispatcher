# astrbot_plugin_xiaomi_mimo_voice_dispatcher

把 Xiaomi MiMo TTS 接入 AstrBot 的函数工具插件，让模型在适合语音表达时调用 `mimo_tts_speak` 直接发送语音消息。

插件聚焦“发语音”，不会从普通聊天消息、语音消息或附件里自动提取克隆样本，避免接入端下载、转码、上传阶段产生大段 base64 日志。

## 功能

- 注册 `mimo_tts_speak` LLM 函数工具，由模型按需发送语音。
- 支持 `builtin`、`voice_design`、`voice_clone` 三种模式。
- 支持管理员为当前会话绑定本地 voice clone 样本。
- 成功发出语音后工具返回 `None`，按 AstrBot 原生机制结束 Agent Loop，避免工具成功后再触发一轮文字收尾。
- 管理命令默认仅管理员可用。

## 配置

至少填写：

- `api_key`: MiMo API Key。

常用可选项：

- `api_base`: MiMo API 地址，默认 `https://token-plan-cn.xiaomimimo.com/v1`。
- `request_timeout_seconds`: 调用 MiMo API 的读取超时时间，默认 `60` 秒。网络不稳定时可调到 `90`，想更快失败可调到 `30`。
- `admin_ids`: 允许使用管理命令的用户 ID。多个 ID 用英文逗号分隔。
- `builtin_model`: 普通 TTS 模型 ID，默认 `mimo-v2.5-tts`。
- `voice_design_model`: 音色设计模型 ID，默认 `mimo-v2.5-tts-voicedesign`。
- `voice_clone_model`: 声音克隆模型 ID，默认 `mimo-v2.5-tts-voiceclone`。
- `builtin_voice`: `builtin` 模式默认内置音色。
- `voice_clone_sample_path`: `voice_clone` 模式默认本地样本路径，支持 `mp3`、`wav`、`m4a`、`flac`、`ogg`。

## 内置音色

- `mimo_default`: 平台默认音色
- `冰糖`: 中文音色
- `茉莉`: 中文音色
- `苏打`: 中文音色
- `白桦`: 中文音色
- `Mia`: 英文女声
- `Chloe`: 英文女声
- `Milo`: 英文男声
- `Dean`: 英文男声

兼容旧配置：如果模型或旧配置传入 `default_zh`，插件会自动映射到 `茉莉`；传入 `default_en` 会自动映射到 `Mia`。

## 使用方式

### 使用内置音色

在插件设置里修改：

```text
builtin_voice = 茉莉
```

模型调用工具时使用：

```text
mimo_tts_speak(mode="builtin", text="要说的话")
```

### 使用配置里的克隆样本

准备一个本地音频文件，然后在插件设置里填写：

```text
voice_clone_sample_path = D:\voice\bot_sample.wav
```

之后用户要求使用预设声音时，模型可以调用：

```text
mimo_tts_speak(mode="voice_clone", text="要说的话")
```

### 管理员临时绑定会话样本

管理员发送：

```text
/mimo_clone_bind D:\voice\sample.wav
```

当前会话会优先使用这份样本。查看和清除：

```text
/mimo_clone_status
/mimo_clone_clear
```

这个方式只接受本地文件路径，不会自动从聊天附件里下载音频。

### 工具调用时指定样本

少量特殊场景可以在工具调用里传本地样本路径：

```text
mimo_tts_speak(
  mode="voice_clone",
  text="今晚早点休息。",
  clone_sample="D:\voice\soft.wav"
)
```

`clone_sample` 只支持本地音频文件路径，不支持 data URI 或 base64。

## 函数工具参数

工具名：

- `mimo_tts_speak`

主要参数：

- `text`: 要合成并发送的文本。
- `mode`: `builtin` / `voice_design` / `voice_clone`。
- `instruction`: 风格指令；在 `voice_design` 模式下作为音色设计描述。
- `voice`: 内置音色 ID，仅 `builtin` 模式使用。
- `clone_sample`: 本地样本路径，仅 `voice_clone` 模式使用。
- `send_text`: 是否同时发送文字。默认 `false`。
- `audio_format`: 当前固定使用 `wav`，这是 AstrBot 发送语音最稳定的格式。

## 管理命令

- `/mimo_tts_status`
- `/mimo_tts_voices`
- `/mimo_tts_say 你好，这是测试语音`
- `/mimo_clone_bind D:\voice\sample.wav`
- `/mimo_clone_status`
- `/mimo_clone_clear`

## AstrBot 机制说明

`mimo_tts_speak` 会在工具内部调用 `event.send()` 直接发送语音。发送成功后工具返回 `None`，这是 AstrBot tool runner 的原生约定：工具没有返回值，或已经把结果直接发送给用户，Agent Loop 会结束。

因此日志里出现类似“`mimo_tts_speak 没有返回值，或者已将结果直接发送给用户`”时通常不是错误，而是插件按预期结束工具循环。

如果模型在调用工具前已经输出了一句文字，插件无法在工具返回阶段撤回那句前置文本；这属于 AstrBot tool loop 对“同一轮文本 + 工具调用”的处理方式。插件会通过系统提示尽量避免这种情况。

## 依赖和边界

- 发送语音固定使用 `wav`。
- `mp3`、`m4a`、`flac`、`ogg` 可作为 voice clone 样本输入格式。
- 非常见格式会尝试用系统 `ffmpeg` 转成 `wav`。如果机器没有安装 `ffmpeg`，相关转码会失败。
- SILK 样本需要可选依赖 `pysilk`，否则无法解码。
- 管理命令的管理员识别优先使用 AstrBot 事件自带判断；如果不生效，请在 `admin_ids` 里显式填写用户 ID。

## 相关链接

- MiMo 官方文档: https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5
- 插件仓库: https://github.com/sl251/astrbot_plugin_xiaomi_mimo_voice_dispatcher
