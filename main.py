import asyncio
import base64
import io
import inspect
import json
import logging
import mimetypes
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import FunctionTool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None

try:
    import pysilk
except ImportError:
    pysilk = None


PLUGIN_NAME = "astrbot_plugin_xiaomi_mimo_voice_dispatcher"
DEFAULT_API_BASE = "https://token-plan-cn.xiaomimimo.com/v1"
DEFAULT_TIMEOUT = 60
DEFAULT_AUDIO_FORMAT = "wav"
DEFAULT_BUILTIN_VOICE = "mimo_default"
DEFAULT_TEST_TEXT = "这是一条来自 Xiaomi MiMo TTS 工具插件的测试语音。"
DEFAULT_PREFER_CLONE_WHEN_CONFIGURED = True
DEFAULT_AUDIO_RETENTION_HOURS = 24
DEFAULT_MAX_CLONE_SAMPLE_MB = 7
ALLOWED_CLONE_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
DISCOVERABLE_CLONE_SUFFIXES = ALLOWED_CLONE_SUFFIXES | {".silk", ".amr"}
INLINE_AUDIO_LOG_RE = re.compile(
    r"(data:audio/[-+.a-zA-Z0-9]+;base64,|base64://)([A-Za-z0-9+/=\r\n]{128,})"
)
LONG_BASE64_LOG_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{512,}={0,2})(?![A-Za-z0-9+/=])")

MODE_TO_MODEL_CONFIG = {
    "builtin": "builtin_model",
    "voice_design": "voice_design_model",
    "voice_clone": "voice_clone_model",
}

MODEL_DEFAULTS = {
    "builtin_model": "mimo-v2.5-tts",
    "voice_design_model": "mimo-v2.5-tts-voicedesign",
    "voice_clone_model": "mimo-v2.5-tts-voiceclone",
}

BUILTIN_VOICES = {
    "mimo_default": "平台默认",
    "冰糖": "中文音色",
    "茉莉": "中文音色",
    "苏打": "中文音色",
    "白桦": "中文音色",
    "Mia": "英文女声",
    "Chloe": "英文女声",
    "Milo": "英文男声",
    "Dean": "英文男声",
}

VOICE_ALIASES = {
    "default_zh": "茉莉",
    "zh": "茉莉",
    "chinese": "茉莉",
    "default_cn": "茉莉",
    "default_en": "Mia",
    "en": "Mia",
    "english": "Mia",
}


def _normalize_mode(mode: str) -> str:
    value = str(mode or "").strip().lower().replace("-", "_")
    aliases = {
        "built_in": "builtin",
        "default": "builtin",
        "tts": "builtin",
        "voicedesign": "voice_design",
        "design": "voice_design",
        "voiceclone": "voice_clone",
        "clone": "voice_clone",
    }
    return aliases.get(value, value or "builtin")


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_filename_extension(audio_format: str) -> str:
    _ = audio_format
    return ".wav"


def _guess_audio_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    if path.suffix.lower() == ".mp3":
        return "audio/mpeg"
    if path.suffix.lower() == ".m4a":
        return "audio/mp4"
    if path.suffix.lower() == ".flac":
        return "audio/flac"
    if path.suffix.lower() == ".ogg":
        return "audio/ogg"
    return "audio/wav"


def _sanitize_filename(name: str, fallback: str = "audio_sample.wav") -> str:
    raw = Path(str(name or "").strip()).name
    if not raw:
        raw = fallback
    sanitized = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in raw)
    return sanitized or fallback


def _redact_inline_audio_for_log(message: str) -> str:
    def _replace_inline(match: re.Match) -> str:
        prefix = match.group(1)
        encoded = match.group(2)
        compact_length = len(encoded.replace("\r", "").replace("\n", ""))
        return f"{prefix}<redacted audio base64, {compact_length} chars>"

    redacted = INLINE_AUDIO_LOG_RE.sub(_replace_inline, message)

    def _replace_bare(match: re.Match) -> str:
        value = match.group(1)
        return f"<redacted base64-like payload, {len(value)} chars>"

    return LONG_BASE64_LOG_RE.sub(_replace_bare, redacted)


@dataclass
class MimoTTSSpeakTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "mimo_tts_speak"
    description: str = (
        "Send a voice message with Xiaomi MiMo TTS. "
        "Use it only when spoken delivery is beneficial. "
        "The synthesized text must be passed in text. "
        "For conversational voice replies, put the full reply into text in one go, "
        "instead of sending extra explanatory text outside the voice. "
        "mode=builtin uses built-in voices, mode=voice_design uses instruction as voice design prompt, "
        "mode=voice_clone uses the configured preset clone sample by default, "
        "or an explicitly provided local clone_sample path."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The exact text to synthesize and send as speech.",
                },
                "mode": {
                    "type": "string",
                    "description": "builtin, voice_design, or voice_clone. Default is builtin.",
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "Optional speaking style instruction. "
                        "For voice_design this becomes the required voice design prompt."
                    ),
                },
                "voice": {
                    "type": "string",
                    "description": "Optional built-in voice ID when mode=builtin, such as mimo_default, 冰糖, 茉莉, 苏打, 白桦, Mia, Chloe, Milo, Dean.",
                },
                "clone_sample": {
                    "type": "string",
                    "description": (
                        "Optional local file path for voice cloning. "
                        "If omitted in voice_clone mode, the configured default voice_clone_sample_path is used."
                    ),
                },
                "send_text": {
                    "type": "boolean",
                    "description": "Whether to also send the text together with the voice. Default follows plugin config.",
                },
                "audio_format": {
                    "type": "string",
                    "description": "Audio format to generate for sending. Only wav is currently supported. Default is wav.",
                    "enum": ["wav"],
                },
            },
            "required": ["text"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        text: str,
        mode: str = "builtin",
        instruction: str = "",
        voice: str = "",
        clone_sample: str = "",
        send_text: bool = False,
        audio_format: str = DEFAULT_AUDIO_FORMAT,
    ):
        return await self.plugin.run_mimo_tts(
            event,
            text=text,
            mode=mode,
            instruction=instruction,
            voice=voice,
            clone_sample=clone_sample,
            send_text=send_text,
            audio_format=audio_format,
        )


class Main(star.Star):
    """MiMo TTS function tool plugin for AstrBot."""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._plugin_data_dir = self._get_plugin_data_dir()
        self._audio_dir = self._plugin_data_dir / "audio_cache"
        self._clone_dir = self._plugin_data_dir / "clone_cache"
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._clone_dir.mkdir(parents=True, exist_ok=True)
        self._session_clone_samples: dict[str, dict[str, Any]] = {}
        self._install_log_noise_filter()
        self._register_llm_tools()

    def _is_known_record_noise_source(self, value: str | None) -> bool:
        if not isinstance(value, str):
            return False
        lowered = value.strip().lower()
        if not lowered:
            return False
        return (
            lowered.startswith("https://multimedia.nt.qq.com.cn/download")
            or lowered.startswith("http://multimedia.nt.qq.com.cn/download")
            or lowered.startswith("data:audio/")
            or lowered.startswith("base64://")
        )

    def _install_log_noise_filter(self):
        root_logger = logging.getLogger()
        if getattr(root_logger, "_mimo_noise_filter_installed", False):
            return

        plugin = self
        original_factory = logging.getLogRecordFactory()

        def _mimo_redacting_record_factory(*args, **kwargs):
            record = original_factory(*args, **kwargs)
            try:
                message = record.getMessage()
            except Exception:
                return record
            if isinstance(message, str):
                redacted = _redact_inline_audio_for_log(message)
                if redacted != message:
                    record.msg = redacted
                    record.args = ()
            return record

        class _MimoAudioLogFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                message = record.getMessage()
                if not isinstance(message, str):
                    return True

                redacted = _redact_inline_audio_for_log(message)
                if redacted != message:
                    record.msg = redacted
                    record.args = ()
                    message = redacted

                if message.startswith("[Media Utils] wav check failed: "):
                    source = message.removeprefix("[Media Utils] wav check failed: ").split(", error:", 1)[0].strip()
                    if plugin._is_known_record_noise_source(source):
                        return False

                if message.startswith("Voice processing failed: "):
                    if (
                        "Server returned 400 Bad Request" in message
                        and "multimedia.nt.qq.com.cn/download" in message
                    ) or ("Invalid data found when processing input" in message and "data:audio/" in message):
                        return False

                return True

        log_filter = _MimoAudioLogFilter()
        root_logger.addFilter(log_filter)
        for handler in root_logger.handlers:
            handler.addFilter(log_filter)
        logging.getLogger("astrbot").addFilter(log_filter)
        logging.setLogRecordFactory(_mimo_redacting_record_factory)
        setattr(root_logger, "_mimo_noise_filter_installed", True)

    def _register_llm_tools(self):
        add_tools = getattr(self.context, "add_llm_tools", None)
        if add_tools is None:
            raise RuntimeError("Current AstrBot version does not support context.add_llm_tools().")
        add_tools(MimoTTSSpeakTool(plugin=self))

    async def terminate(self) -> None:
        unregister_tool = getattr(self.context, "unregister_llm_tool", None)
        if callable(unregister_tool):
            try:
                unregister_tool("mimo_tts_speak")
                return
            except Exception as exc:
                logger.warning(f"[MiMo TTS] failed to unregister LLM tool via context: {exc}")

        get_tool_manager = getattr(self.context, "get_llm_tool_manager", None)
        if callable(get_tool_manager):
            try:
                manager = get_tool_manager()
                remove_func = getattr(manager, "remove_func", None)
                if callable(remove_func):
                    remove_func("mimo_tts_speak")
            except Exception as exc:
                logger.warning(f"[MiMo TTS] failed to remove LLM tool from manager: {exc}")

    def _get_plugin_data_dir(self) -> Path:
        if get_astrbot_data_path is not None:
            try:
                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            except Exception:
                pass
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        return (
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or event.get_sender_id()
        )

    def _api_base(self) -> str:
        return str(self.config.get("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE).rstrip("/")

    def _api_endpoint(self) -> str:
        base = self._api_base()
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _api_key(self) -> str:
        return str(self.config.get("api_key", "") or "").strip()

    def _timeout(self) -> int:
        return _safe_int(
            self.config.get("request_timeout_seconds", DEFAULT_TIMEOUT),
            DEFAULT_TIMEOUT,
            minimum=5,
            maximum=110,
        )

    def _default_send_text(self) -> bool:
        return False

    def _prefer_clone_when_configured(self) -> bool:
        return _safe_bool(
            self.config.get("prefer_clone_when_sample_configured"),
            DEFAULT_PREFER_CLONE_WHEN_CONFIGURED,
        )

    def _open_direct(self, request: urllib.request.Request):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=self._timeout())

    def _default_builtin_voice(self) -> str:
        return self._normalize_builtin_voice(self.config.get("builtin_voice", DEFAULT_BUILTIN_VOICE))

    def _normalize_builtin_voice(self, voice: Any) -> str:
        raw = str(voice or "").strip()
        if not raw:
            return DEFAULT_BUILTIN_VOICE
        alias = VOICE_ALIASES.get(raw.lower())
        if alias:
            return alias
        if raw in BUILTIN_VOICES:
            return raw
        logger.warning(f"[MiMo TTS] unknown builtin voice {raw!r}, fallback to {DEFAULT_BUILTIN_VOICE}")
        return DEFAULT_BUILTIN_VOICE

    def _configured_admin_ids(self) -> set[str]:
        raw = (
            self.config.get("admin_ids")
            or self.config.get("admins")
            or self.config.get("administrator_ids")
            or ""
        )
        if isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = str(raw or "").replace(";", ",").split(",")
        return {str(value).strip() for value in values if str(value).strip()}

    async def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        for attr in ("is_admin", "is_admin_event"):
            checker = getattr(event, attr, None)
            if callable(checker):
                try:
                    result = checker()
                    if inspect.isawaitable(result):
                        result = await result
                    if result:
                        return True
                except Exception:
                    pass

        sender_id = str(event.get_sender_id() or "").strip()
        configured_admins = self._configured_admin_ids()
        if configured_admins:
            return sender_id in configured_admins

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("role", "permission", "user_role"):
            value = str(getattr(sender, attr, "") or "").lower()
            if value in {"admin", "administrator", "owner", "superuser"}:
                return True
        return False

    async def _require_admin(self, event: AstrMessageEvent) -> bool:
        if await self._is_admin_event(event):
            return True
        await event.send(event.plain_result("这个 MiMo TTS 管理命令仅允许管理员使用。"))
        return False

    def _audio_retention_hours(self) -> int:
        return DEFAULT_AUDIO_RETENTION_HOURS

    def _max_clone_sample_bytes(self) -> int:
        return DEFAULT_MAX_CLONE_SAMPLE_MB * 1024 * 1024

    def _resolve_model_name(self, mode: str) -> str:
        config_key = MODE_TO_MODEL_CONFIG[mode]
        return str(self.config.get(config_key, MODEL_DEFAULTS[config_key]) or MODEL_DEFAULTS[config_key]).strip()

    def _cleanup_audio_cache(self):
        cutoff = time.time() - self._audio_retention_hours() * 3600
        protected_paths = set()
        for meta in self._session_clone_samples.values():
            try:
                protected_paths.add(Path(str(meta.get("path", ""))).resolve())
            except OSError:
                continue
        for base_dir in (self._audio_dir, self._clone_dir):
            try:
                if not base_dir.exists():
                    continue
                for path in base_dir.rglob("*"):
                    if path.is_dir():
                        continue
                    try:
                        resolved = path.resolve()
                    except OSError:
                        continue
                    if resolved in protected_paths:
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(f"[MiMo TTS] Failed to cleanup cache in {base_dir}: {exc}")

    def _is_plugin_cache_path(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for base_dir in (self._audio_dir, self._clone_dir):
            try:
                resolved.relative_to(base_dir.resolve())
                return True
            except ValueError:
                continue
        return False

    def _cleanup_transient_paths(self, *paths: Path | None, keep: Path | None = None):
        keep_resolved = None
        if keep is not None:
            try:
                keep_resolved = keep.resolve()
            except OSError:
                keep_resolved = None

        seen: set[Path] = set()
        for path in paths:
            if path is None:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if keep_resolved is not None and resolved == keep_resolved:
                continue
            if not self._is_plugin_cache_path(resolved):
                continue
            try:
                if resolved.exists() and resolved.is_file():
                    resolved.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(f"[MiMo TTS] Failed to cleanup transient cache file {resolved}: {exc}")

    def _replace_session_clone_sample(self, session_id: str, meta: dict[str, Any]):
        previous = self._session_clone_samples.get(session_id)
        previous_path = None
        if previous:
            previous_path = Path(str(previous.get("path", "")))
        self._session_clone_samples[session_id] = meta
        current_path = Path(str(meta.get("path", "")))
        self._cleanup_transient_paths(previous_path, keep=current_path)

    def _clear_session_clone_sample(self, session_id: str) -> dict[str, Any] | None:
        meta = self._session_clone_samples.pop(session_id, None)
        if not meta:
            return None
        cached_path = Path(str(meta.get("path", "")))
        self._cleanup_transient_paths(cached_path)
        return meta

    def _save_audio_bytes(self, audio_bytes: bytes, audio_format: str) -> Path:
        self._cleanup_audio_cache()
        suffix = _safe_filename_extension(audio_format)
        file_name = f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
        target = self._audio_dir / file_name
        target.write_bytes(audio_bytes)
        return target

    def _resolve_session_clone_sample_path(self, event: AstrMessageEvent) -> str:
        session_id = self._get_session_id(event)
        meta = self._session_clone_samples.get(session_id)
        if meta:
            path = Path(str(meta.get("path", "")))
            if path.exists() and path.is_file():
                return str(path)

        configured = str(self.config.get("voice_clone_sample_path", "") or "").strip()
        if configured:
            return configured
        raise ValueError("voice_clone 模式需要先在插件配置里填写 voice_clone_sample_path，或在工具调用里显式传入本地 clone_sample 路径。")

    def _has_clone_sample_for_event(self, event: AstrMessageEvent) -> bool:
        try:
            self._resolve_session_clone_sample_path(event)
            return True
        except ValueError:
            return False

    def _select_clone_sample_from_directory(self, directory: Path) -> Path:
        resolved_dir = directory.expanduser().resolve()
        if not resolved_dir.exists() or not resolved_dir.is_dir():
            raise ValueError(f"voice clone 样本目录不存在: {resolved_dir}")

        candidates = [
            path
            for path in resolved_dir.iterdir()
            if path.is_file() and path.suffix.lower() in DISCOVERABLE_CLONE_SUFFIXES
        ]
        if not candidates:
            raise ValueError(f"voice clone 样本目录里没有可用音频文件: {resolved_dir}")

        candidates.sort(key=lambda path: (path.stat().st_mtime, path.name.lower()), reverse=True)
        selected = candidates[0]
        if len(candidates) > 1:
            logger.info(f"[MiMo TTS] selected newest clone sample from directory: {selected}")
        return selected

    def _resolve_clone_sample_path_value(self, raw_value: str) -> Path:
        sample_path = Path(str(raw_value or "").strip()).expanduser()
        if sample_path.is_dir():
            sample_path = self._select_clone_sample_from_directory(sample_path)
        return sample_path

    def _transcode_clone_sample_to_wav(self, source_path: Path, target_dir: Path | None = None) -> Path:
        resolved_source = source_path.expanduser().resolve()
        if not resolved_source.exists() or not resolved_source.is_file():
            raise ValueError(f"voice clone 样本文件不存在: {resolved_source}")

        normalized_target_dir = (target_dir or self._clone_dir).resolve()
        normalized_target_dir.mkdir(parents=True, exist_ok=True)
        forced_input_format = self._guess_ffmpeg_input_format(resolved_source)
        if forced_input_format == "silk":
            return self._decode_silk_clone_sample_to_wav(
                resolved_source,
                target_dir=normalized_target_dir,
            )

        target_path = normalized_target_dir / f"normalized_{int(time.time())}_{uuid.uuid4().hex}.wav"
        command = ["ffmpeg", "-y"]
        if forced_input_format:
            command.extend(["-f", forced_input_format])
        command.extend(
            [
                "-i",
                str(resolved_source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "24000",
                str(target_path),
            ]
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not target_path.exists() or target_path.stat().st_size <= 0:
            stderr = (result.stderr or "").strip() or (result.stdout or "").strip() or "unknown ffmpeg error"
            raise RuntimeError(f"failed to transcode clone sample with ffmpeg: {stderr}")
        return target_path

    def _ensure_supported_clone_sample_path(
        self,
        source_path: Path,
        *,
        target_dir: Path | None = None,
    ) -> Path:
        resolved_source = source_path.expanduser().resolve()
        if resolved_source.is_dir():
            resolved_source = self._select_clone_sample_from_directory(resolved_source)
        if resolved_source.suffix.lower() in ALLOWED_CLONE_SUFFIXES:
            return resolved_source
        return self._transcode_clone_sample_to_wav(resolved_source, target_dir=target_dir)

    def _resolve_clone_sample_data_uri(self, event: AstrMessageEvent, clone_sample: str) -> str:
        raw_value = str(clone_sample or "").strip()
        if not raw_value:
            raw_value = self._resolve_session_clone_sample_path(event)

        if raw_value.startswith("data:") and ";base64," in raw_value:
            raise ValueError("clone_sample 只支持本地音频文件路径；请不要传入 data URI 或 base64 内容。")

        sample_path = self._resolve_clone_sample_path_value(raw_value)
        sample_path = self._ensure_supported_clone_sample_path(sample_path)
        if not sample_path.is_absolute():
            sample_path = Path.cwd() / sample_path
        if not sample_path.exists() or not sample_path.is_file():
            raise ValueError(f"voice clone 样本文件不存在: {sample_path}")

        sample_bytes = sample_path.read_bytes()
        if len(sample_bytes) > self._max_clone_sample_bytes():
            raise ValueError("voice clone 样本过大，请换一个更短的 mp3/wav 音频。")

        encoded = base64.b64encode(sample_bytes).decode("utf-8")
        mime_type = _guess_audio_mime(sample_path)
        return f"data:{mime_type};base64,{encoded}"

    def _build_messages(self, text: str, mode: str, instruction: str) -> list[dict[str, str]]:
        spoken_text = str(text or "").strip()
        style_instruction = str(instruction or "").strip()

        if not spoken_text:
            raise ValueError("text 不能为空。")
        if mode == "voice_design" and not style_instruction:
            raise ValueError("voice_design 模式下 instruction 不能为空，它会作为音色设计描述。")

        messages: list[dict[str, str]] = []
        if style_instruction:
            messages.append({"role": "user", "content": style_instruction})
        elif mode == "voice_clone":
            messages.append({"role": "user", "content": ""})

        messages.append({"role": "assistant", "content": spoken_text})
        return messages

    def _build_payload(
        self,
        *,
        event: AstrMessageEvent,
        text: str,
        mode: str,
        instruction: str,
        voice: str,
        clone_sample: str,
        audio_format: str,
    ) -> dict[str, Any]:
        normalized_mode = _normalize_mode(mode)
        if (
            normalized_mode == "builtin"
            and not str(voice or "").strip()
            and (str(clone_sample or "").strip() or self._prefer_clone_when_configured())
            and (str(clone_sample or "").strip() or self._has_clone_sample_for_event(event))
        ):
            normalized_mode = "voice_clone"

        if normalized_mode not in MODE_TO_MODEL_CONFIG:
            raise ValueError("mode 仅支持 builtin、voice_design、voice_clone。")

        normalized_format = str(audio_format or DEFAULT_AUDIO_FORMAT).strip().lower()
        if normalized_format != "wav":
            raise ValueError("audio_format 目前仅支持 wav，以确保 AstrBot 能稳定发送语音。")

        payload: dict[str, Any] = {
            "model": self._resolve_model_name(normalized_mode),
            "messages": self._build_messages(text=text, mode=normalized_mode, instruction=instruction),
            "audio": {
                "format": normalized_format,
            },
        }

        if normalized_mode == "builtin":
            payload["audio"]["voice"] = self._normalize_builtin_voice(voice or self._default_builtin_voice())
        elif normalized_mode == "voice_clone":
            payload["audio"]["voice"] = self._resolve_clone_sample_data_uri(event, clone_sample)

        return payload

    async def _request_tts(self, payload: dict[str, Any]) -> bytes:
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("未配置 api_key。")

        request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "api-key": api_key,
        }
        request = urllib.request.Request(
            self._api_endpoint(),
            data=request_body,
            headers=headers,
            method="POST",
        )

        def _do_request() -> bytes:
            with self._open_direct(request) as response:
                raw = response.read()

            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise RuntimeError(f"MiMo 返回了无法解析的响应: {exc}") from exc

            error_obj = data.get("error")
            if error_obj:
                raise RuntimeError(f"MiMo 接口错误: {json.dumps(error_obj, ensure_ascii=False)}")

            try:
                audio_b64 = data["choices"][0]["message"]["audio"]["data"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    "MiMo 响应中没有找到 audio.data，请检查模型、配额或请求参数。"
                ) from exc

            try:
                return base64.b64decode(audio_b64)
            except Exception as exc:
                raise RuntimeError(f"音频 Base64 解码失败: {exc}") from exc

        try:
            return await asyncio.to_thread(_do_request)
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"MiMo 请求读取超时，请检查网络、api_base 或稍后重试。当前超时: {self._timeout()} 秒。") from exc
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = str(exc)
            raise RuntimeError(f"MiMo HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RuntimeError(
                    f"MiMo 请求读取超时，请检查网络、api_base 或稍后重试。当前超时: {self._timeout()} 秒。"
                ) from exc
            raise RuntimeError(f"MiMo 请求失败: {exc.reason}") from exc

    async def _send_voice_result(self, event: AstrMessageEvent, audio_path: Path, text: str, send_text: bool):
        record_cls = getattr(Comp, "Record", None)
        plain_cls = getattr(Comp, "Plain", None)

        if record_cls is None:
            raise RuntimeError("当前 AstrBot 版本缺少 Comp.Record，无法发送语音。")

        chain = []
        if send_text and plain_cls is not None:
            chain.append(plain_cls(text=text))
        chain.append(record_cls(file=str(audio_path.resolve())))
        await event.send(event.chain_result(chain))

    def _guess_ffmpeg_input_format(self, source_path: Path) -> str | None:
        try:
            header = source_path.read_bytes()[:64]
        except OSError:
            return None

        if b"#!SILK_V3" in header:
            return "silk"
        if b"#!AMR-WB" in header:
            return "amrwb"
        if b"#!AMR" in header:
            return "amr"
        return None

    def _decode_silk_clone_sample_to_wav(
        self,
        source_path: Path,
        *,
        target_dir: Path | None = None,
        sample_rate: int = 24000,
    ) -> Path:
        if pysilk is None:
            raise RuntimeError("pysilk is unavailable for silk clone sample decoding")

        normalized_target_dir = (target_dir or self._clone_dir).resolve()
        normalized_target_dir.mkdir(parents=True, exist_ok=True)
        target_path = normalized_target_dir / f"normalized_{int(time.time())}_{uuid.uuid4().hex}.wav"

        pcm_buffer = io.BytesIO()
        with source_path.open("rb") as silk_file:
            pysilk.decode(silk_file, pcm_buffer, sample_rate=sample_rate)

        pcm_bytes = pcm_buffer.getvalue()
        if not pcm_bytes:
            raise RuntimeError("decoded silk clone sample is empty")

        with wave.open(str(target_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)

        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise RuntimeError("failed to materialize decoded silk clone sample as wav")
        return target_path

    async def run_mimo_tts(
        self,
        event: AstrMessageEvent,
        *,
        text: str,
        mode: str = "builtin",
        instruction: str = "",
        voice: str = "",
        clone_sample: str = "",
        send_text: bool = False,
        audio_format: str = DEFAULT_AUDIO_FORMAT,
    ) -> None:
        payload = self._build_payload(
            event=event,
            text=text,
            mode=mode,
            instruction=instruction,
            voice=voice,
            clone_sample=clone_sample,
            audio_format=audio_format,
        )
        audio_bytes = await self._request_tts(payload)
        audio_path = self._save_audio_bytes(audio_bytes, audio_format)
        final_send_text = _safe_bool(send_text, self._default_send_text())
        await self._send_voice_result(
            event,
            audio_path=audio_path,
            text=str(text or "").strip(),
            send_text=final_send_text,
        )
        if hasattr(event, "clear_result"):
            event.clear_result()
        if hasattr(event, "stop_event"):
            event.stop_event()
        return None

    @filter.on_llm_request()
    async def inject_mimo_tts_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        if self._api_key():
            clone_sample = str(self.config.get("voice_clone_sample_path", "") or "").strip()
            hint = (
                "\n\n[System Notice] A MiMo TTS tool named mimo_tts_speak is available. "
                "Use it only when spoken delivery adds value, such as朗读、播报、模仿角色口吻、情绪化台词、欢迎词、祝福语、配音片段。 "
                "Do not call it for ordinary factual answers, long technical explanations, code, tables, or when the user did not ask for audio-like delivery. "
                "When using mimo_tts_speak, pass the exact spoken text in text. "
                "For builtin mode, use only these voice ids: mimo_default, 冰糖, 茉莉, 苏打, 白桦, Mia, Chloe, Milo, Dean. "
                "Use mode=builtin for normal built-in voices, mode=voice_design when you need a brand-new voice from description, "
                "and mode=voice_clone only when a preset clone sample is configured or the tool call includes an explicit local clone_sample path. "
                f"If the user does not specify a format, prefer audio_format={DEFAULT_AUDIO_FORMAT}. "
                "Set send_text=true only when the user also needs the text alongside the voice. "
                "When the user wants a spoken reply, put the full reply into one mimo_tts_speak call. "
                "Do not first send an explanatory text message, and do not add a follow-up text summary after the tool succeeds, "
                "unless the user explicitly asks for transcript or text alongside the audio. "
                "If the tool fails and the user asked for voice-only delivery, do not send the intended spoken text as a fallback."
            )
            if clone_sample:
                hint += (
                    "\n[System Notice] A preset voice-clone sample is configured in voice_clone_sample_path. "
                    "Use mode=voice_clone for normal spoken replies unless the user explicitly asks for a built-in voice. "
                    "You do not need to pass clone_sample unless the user explicitly wants a different sample. "
                    "If voice_clone_sample_path is a directory, the plugin will use the newest supported audio file in it."
                )
        else:
            hint = (
                "\n\n[System Notice] The MiMo TTS tool exists but api_key is not configured. "
                "Do not call mimo_tts_speak until the plugin is configured."
            )

        current_prompt = getattr(req, "system_prompt", "") or ""
        req.system_prompt = current_prompt + hint
        _ = event

    def _get_session_clone_meta(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        session_id = self._get_session_id(event)
        meta = self._session_clone_samples.get(session_id)
        if not meta:
            return None
        path = Path(str(meta.get("path", "")))
        if not path.exists():
            self._clear_session_clone_sample(session_id)
            return None
        return meta

    @filter.command("mimo_tts_status", priority=1)
    async def mimo_tts_status(self, event: AstrMessageEvent):
        if not await self._require_admin(event):
            return
        clone_sample = str(self.config.get("voice_clone_sample_path", "") or "").strip()
        message = (
            f"MiMo API Base: {self._api_base()}\n"
            f"API Key 已配置: {'是' if bool(self._api_key()) else '否'}\n"
            f"默认内置音色: {self._default_builtin_voice()}\n"
            f"已配置默认 voice clone 样本: {'是' if bool(clone_sample) else '否'}\n"
            f"配置样本后默认使用克隆音色: {'是' if self._prefer_clone_when_configured() else '否'}\n"
            "会话自动绑定样本: 已禁用"
        )
        yield event.plain_result(message)

    @filter.command("mimo_tts_voices", priority=1)
    async def mimo_tts_voices(self, event: AstrMessageEvent):
        if not await self._require_admin(event):
            return
        voice_lines = [f"- {voice_id}: {desc}" for voice_id, desc in BUILTIN_VOICES.items()]
        message = "MiMo 内置可选音色:\n" + "\n".join(voice_lines)
        yield event.plain_result(message)

    @filter.command("mimo_clone_bind", priority=1)
    async def mimo_clone_bind(self, event: AstrMessageEvent, *, sample_path: str = ""):
        if not await self._require_admin(event):
            return
        raw_path = str(sample_path or "").strip()
        if not raw_path:
            yield event.plain_result("请提供本地样本路径，例如 /mimo_clone_bind D:\\voice\\sample.wav。")
            return

        try:
            prepared_path = self._ensure_supported_clone_sample_path(self._resolve_clone_sample_path_value(raw_path))
            sample_bytes = prepared_path.read_bytes()
            if len(sample_bytes) > self._max_clone_sample_bytes():
                yield event.plain_result("voice clone 样本过大，请换一个更短的 mp3/wav 音频。")
                return

            session_id = self._get_session_id(event)
            session_dir = self._clone_dir / _sanitize_filename(session_id, "session")
            session_dir.mkdir(parents=True, exist_ok=True)
            suffix = prepared_path.suffix.lower() if prepared_path.suffix.lower() in ALLOWED_CLONE_SUFFIXES else ".wav"
            target_path = session_dir / f"admin_{int(time.time())}_{uuid.uuid4().hex}{suffix}"
            target_path.write_bytes(sample_bytes)
            meta = {
                "path": str(target_path.resolve()),
                "source_name": Path(raw_path).name or str(prepared_path.name),
                "updated_at": time.time(),
            }
            self._replace_session_clone_sample(session_id, meta)
            self._cleanup_transient_paths(prepared_path, keep=target_path)
        except Exception as exc:
            yield event.plain_result(f"绑定失败: {exc}")
            return

        yield event.plain_result(f"已绑定当前会话的预设 voice clone 样本: {meta.get('source_name')}")

    @filter.command("mimo_clone_clear", priority=1)
    async def mimo_clone_clear(self, event: AstrMessageEvent):
        if not await self._require_admin(event):
            return
        session_id = self._get_session_id(event)
        meta = self._clear_session_clone_sample(session_id)
        if meta:
            yield event.plain_result("当前会话的预设 voice clone 样本已清除。")
            return
        yield event.plain_result("当前会话没有已绑定的预设 voice clone 样本。")

    @filter.command("mimo_clone_status", priority=1)
    async def mimo_clone_status(self, event: AstrMessageEvent):
        if not await self._require_admin(event):
            return
        meta = self._get_session_clone_meta(event)
        configured = str(self.config.get("voice_clone_sample_path", "") or "").strip()
        if not meta:
            if configured:
                yield event.plain_result(f"当前使用插件配置里的 voice clone 样本: {configured}")
                return
            yield event.plain_result("当前没有预设 voice clone 样本。请在插件设置里填写 voice_clone_sample_path，或由管理员用 /mimo_clone_bind 绑定本地路径。")
            return
        yield event.plain_result(
            f"当前会话已绑定预设 voice clone 样本: {meta.get('source_name')}\n"
            "之后模型在需要模仿这个声音时，可以直接调用 mode=voice_clone。"
        )

    @filter.command("mimo_tts_say", priority=1)
    async def mimo_tts_say(self, event: AstrMessageEvent, *, text: str = DEFAULT_TEST_TEXT):
        if not await self._require_admin(event):
            return
        spoken_text = str(text or "").strip() or DEFAULT_TEST_TEXT
        try:
            await self.run_mimo_tts(
                event,
                text=spoken_text,
                mode="builtin",
                instruction="",
                voice="",
                clone_sample="",
                send_text=True,
                audio_format=DEFAULT_AUDIO_FORMAT,
            )
        except Exception as exc:
            logger.error(f"[MiMo TTS] mimo_tts_say failed: {exc}")
            yield event.plain_result(f"发送失败: {exc}")
            return

        yield event.plain_result("MiMo TTS 测试语音已发送。")
