# astrbot_plugin_xiaomi_mimo_voice_dispatcher

为 AstrBot 提供 Xiaomi MiMo TTS 语音合成能力的函数工具插件。

> **💡 开发缘起**
> 前段时间小米开放了免费一个月的 Token Plan API 额度申请活动，里面居然有免费tts模型，这下不得不用了，但是插件市场目前的语音插件貌似不能让模型自己发声，所以这个插件就来了

本插件的核心设计理念是“把发语音的决定权交给模型”。它不仅仅是一个被动的文字转语音（TTS）转换器，而是让大模型在对话中感知到情绪波动、需要表达特定语气，或者认为当前语境（如晚安问候、长篇故事、情感安抚）更适合用语音沟通时，能够自主调用 `mimo_tts_speak` 工具，直接对用户开口说话，带来更生动、拟人的交互体验。

## ✨ 核心功能

* **🤖 模型自主决策**：注册 `mimo_tts_speak` 函数工具，模型根据上下文语境，自主判断在最恰当的时机发送语音消息。
* **🎛️ 多模式支持**：完整支持 `builtin`（内置）、`voice_design`（音色设计）、`voice_clone`（声音克隆）三种合成模式。
* **🎯 会话级克隆**：支持管理员为当前特定会话动态绑定本地 Voice Clone 音频样本，实现定制化对话。
* **🔒 权限管控**：管理命令默认仅管理员可用。

---

## ⚙️ 插件配置

### 必填项

* **`api_key`**: 你的 MiMo API Key。

### 常用可选项

* **`api_base`**: MiMo API 地址，默认 `[https://token-plan-cn.xiaomimimo.com/v1](https://token-plan-cn.xiaomimimo.com/v1)`。
* **`request_timeout_seconds`**: API 读取超时时间，默认 `60` 秒。网络不稳定时可调至 `90`，需快速失败可调至 `30`。
* **`admin_ids`**: 允许使用管理命令的用户 ID 列表（英文逗号分隔）。优先使用 AstrBot 事件自带的管理员判断，如不生效请在此显式填写。
* **模型设置**:
* `builtin_model`: 普通 TTS 模型 ID（默认 `mimo-v2.5-tts`）。
* `voice_design_model`: 音色设计模型 ID（默认 `mimo-v2.5-tts-voicedesign`）。
* `voice_clone_model`: 声音克隆模型 ID（默认 `mimo-v2.5-tts-voiceclone`）。


* **默认音色/样本**:
* `builtin_voice`: `builtin` 模式的默认内置音色名称，只影响内置音色，不会自动使用克隆样本。
* `prefer_clone_when_sample_configured`: 填写克隆样本路径后，普通语音调用默认使用 `voice_clone`。模型明确指定内置音色时仍会使用 `builtin`。
* `voice_clone_sample_path`: `voice_clone` 模式的默认本地样本路径。可以填写单个音频文件，也可以填写文件夹；填写文件夹时会自动使用其中最新的 `mp3`, `wav`, `m4a`, `flac`, `ogg` 样本。



---

## 🎙️ 内置音色映射表

| 音色名称 | 适用语言 | 备注 |
| --- | --- | --- |
| `mimo_default` | 中/英 | 平台默认音色 |
| `冰糖` / `茉莉` / `苏打` / `白桦` | 中文 | 中文女声/男声等 |
| `Mia` / `Chloe` | 英文 | 英文女声 |
| `Milo` / `Dean` | 英文 | 英文男声 |

> **💡 兼容性提示**：如果模型或旧版配置传入了 `default_zh`，插件会自动映射到 `茉莉`；传入 `default_en` 会自动映射到 `Mia`。

---

## 🛠️ 函数工具参数 (`mimo_tts_speak`)

当大模型决定发送语音时，会调用此工具。参数定义如下：

| 参数名 | 必填 | 说明 |
| --- | --- | --- |
| `text` | 是 | 要合成并发送的文本内容。 |
| `mode` | 是 | 合成模式：`builtin` / `voice_design` / `voice_clone`。 |
| `instruction` | 否 | 风格指令；在 `voice_design` 模式下作为音色设计的提示词描述。 |
| `voice` | 否 | 内置音色 ID（仅 `builtin` 模式使用）。 |
| `clone_sample` | 否 | 本地音频样本的绝对路径（仅 `voice_clone` 模式使用），**不支持 data URI 或 base64**。 |
| `send_text` | 否 | 是否同时发送对应的文字消息，默认 `false`。 |
| `audio_format` | 否 | 当前固定使用 `wav`（AstrBot 发送语音最稳定的格式）。 |

---

## 🚀 使用指南

### 1. 使用内置音色 (`builtin`)

在插件设置面板填写默认音色：

```ini
builtin_voice = 茉莉

```

模型在回复时即可直接调用：

```python
mimo_tts_speak(mode="builtin", text="要说的话")

```

### 2. 使用全局克隆样本 (`voice_clone`)

准备好本地音频文件，在插件设置中配置路径。可以填单个文件：

```ini
voice_clone_sample_path = D:\voice\bot_sample.wav

```

也可以填文件夹，插件会自动选择里面最新的支持格式音频：

```ini
voice_clone_sample_path = D:\voice

```

默认情况下，只要配置了 `voice_clone_sample_path`，模型普通调用语音工具时会优先使用这个克隆样本。仍然也可以显式调用：

```python
mimo_tts_speak(mode="voice_clone", text="要说的话")

```

### 3. 会话级动态绑定（管理员功能）

管理员可在聊天中直接为**当前会话**绑定优先使用的克隆样本（仅支持服务器本地文件路径）：

* 绑定：`/mimo_clone_bind D:\voice\sample.wav`
* 查看当前状态：`/mimo_clone_status`
* 清除绑定：`/mimo_clone_clear`

### 4. 工具调用时指定特殊样本

在少量特殊场景下，大模型可以主动传入指定的本地路径来发声：

```python
mimo_tts_speak(
  mode="voice_clone",
  text="今晚早点休息。",
  clone_sample="D:\voice\soft.wav"
)

```

---

## 💻 管理命令一览

* `/mimo_tts_status` - 查看当前 TTS 服务状态与配置
* `/mimo_tts_voices` - 列出所有可用的内置音色
* `/mimo_tts_say <内容>` - 手动测试语音合成
* `/mimo_clone_bind <本地路径>` - 为当前会话绑定克隆样本
* `/mimo_clone_status` - 查看当前会话的克隆样本绑定情况
* `/mimo_clone_clear` - 清除当前会话的克隆样本绑定

---

## 📌 技术细节与边界

1. **工具执行机制**：
* `mimo_tts_speak` 会在工具内部直接调用 `event.send()` 发送语音。发送成功后工具会返回 `None`。
* **注意**：如果你在日志中看到 *“mimo_tts_speak 没有返回值，或者已将结果直接发送给用户”*，这**并非报错**，而是预期行为，意味着模型的一轮语音表达已经完整结束。
* 如果大模型在调用工具前已经生成了一段文字（同一轮交互内），插件无法撤回那段前置文字。


2. **音频格式与依赖**：
* 最终发送给用户的语音固定使用 `wav` 格式。
* `mp3`, `m4a`, `flac`, `ogg` 均可作为克隆样本的输入。
* 非常见格式插件会尝试调用系统 `ffmpeg` 转为 `wav`。**需确保宿主机已安装 `ffmpeg**`，否则转码会失败。
* 如果样本为 SILK 格式，需要环境中安装可选依赖 `pysilk`，否则无法解码。



---

## 🔗 相关链接

* [MiMo 官方文档](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5)
* [插件 GitHub 仓库](https://github.com/sl251/astrbot_plugin_xiaomi_mimo_voice_dispatcher)
