"""Parse exported chat transcripts into turns for memory import."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Literal


ImportFormat = Literal["json_messages", "markdown", "plain_text", "unknown"]

MAX_TURNS = 50
_MAX_CHARS_PER_TURN = 8000
_MAX_TOTAL_CHARS = 200_000
_ROLE_ALIASES = {
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "system": "system",
    "tool": "tool",
}


@dataclass(frozen=True)
class ConversationTurn:
    index: int
    user_text: str
    assistant_text: str | None = None


@dataclass(frozen=True)
class ConversationImportPreview:
    format: ImportFormat
    turn_count: int
    total_chars: int
    truncated: bool
    turns: list[ConversationTurn]
    warnings: list[str]


def parse_conversation_import(
    raw: str,
    *,
    max_turns: int = MAX_TURNS,
) -> ConversationImportPreview:
    text = (raw or "").strip()
    warnings: list[str] = []
    if not text:
        raise ValueError("导入内容为空")
    if len(text) > _MAX_TOTAL_CHARS:
        raise ValueError(f"导入内容超过 {_MAX_TOTAL_CHARS} 字符上限")

    turns: list[ConversationTurn] = []
    detected: ImportFormat = "unknown"

    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            turns = _turns_from_json(payload)
            detected = "json_messages"
            if not turns:
                warnings.append("JSON 已解析，但未找到 user 消息")

    if not turns:
        md_turns = _turns_from_role_lines(text)
        if md_turns:
            turns = md_turns
            detected = "markdown" if re.search(r"^#{1,6}\s", text, re.M) else "plain_text"
        else:
            # Fallback: treat whole document as a single user utterance.
            turns = [ConversationTurn(index=0, user_text=text[:_MAX_CHARS_PER_TURN])]
            detected = "plain_text"
            warnings.append("未能识别角色标记，已将全文当作单条用户消息")

    truncated = False
    if len(turns) > max_turns:
        turns = turns[:max_turns]
        truncated = True
        warnings.append(f"仅预览/导入前 {max_turns} 轮用户发言")

    normalized: list[ConversationTurn] = []
    for turn in turns:
        user_text = turn.user_text.strip()
        if not user_text:
            continue
        if len(user_text) > _MAX_CHARS_PER_TURN:
            user_text = user_text[:_MAX_CHARS_PER_TURN]
            warnings.append(f"第 {turn.index + 1} 轮用户文本已截断至 {_MAX_CHARS_PER_TURN} 字符")
        assistant = (turn.assistant_text or "").strip() or None
        if assistant and len(assistant) > _MAX_CHARS_PER_TURN:
            assistant = assistant[:_MAX_CHARS_PER_TURN]
        normalized.append(
            ConversationTurn(
                index=len(normalized),
                user_text=user_text,
                assistant_text=assistant,
            )
        )

    if not normalized:
        raise ValueError("未解析到可导入的用户消息")

    total_chars = sum(
        len(turn.user_text) + len(turn.assistant_text or "") for turn in normalized
    )
    return ConversationImportPreview(
        format=detected,
        turn_count=len(normalized),
        total_chars=total_chars,
        truncated=truncated,
        turns=normalized,
        warnings=warnings,
    )


def _turns_from_json(payload: Any) -> list[ConversationTurn]:
    messages = _extract_messages(payload)
    if not messages:
        return []
    return _pair_messages(messages)


def _extract_messages(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, list):
        return [_normalize_message(item) for item in payload if _normalize_message(item)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "data", "conversation", "chats", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [
                msg for item in value if (msg := _normalize_message(item))
            ]
    # Single message object
    single = _normalize_message(payload)
    return [single] if single else []


def _normalize_message(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    role_raw = str(item.get("role") or item.get("from") or item.get("speaker") or "").strip()
    role = _ROLE_ALIASES.get(role_raw.lower())
    if role not in {"user", "assistant"}:
        return None
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        content = "\n".join(parts)
    if not isinstance(content, str):
        content = str(item.get("text") or item.get("message") or "")
    content = content.strip()
    if not content:
        return None
    return {"role": role, "content": content}


def _pair_messages(messages: list[dict[str, str]]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    pending_user: str | None = None
    for message in messages:
        if message["role"] == "user":
            if pending_user is not None:
                turns.append(
                    ConversationTurn(index=len(turns), user_text=pending_user)
                )
            pending_user = message["content"]
        elif message["role"] == "assistant" and pending_user is not None:
            turns.append(
                ConversationTurn(
                    index=len(turns),
                    user_text=pending_user,
                    assistant_text=message["content"],
                )
            )
            pending_user = None
    if pending_user is not None:
        turns.append(ConversationTurn(index=len(turns), user_text=pending_user))
    return turns


_ROLE_LINE_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:[*_]{0,2})(user|human|assistant|ai|bot|你|用户|助手)"
    r"(?:[*_]{0,2})\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)


def _turns_from_role_lines(text: str) -> list[ConversationTurn]:
    messages: list[dict[str, str]] = []
    current_role: str | None = None
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_parts
        if current_role and current_parts:
            content = "\n".join(current_parts).strip()
            if content:
                messages.append({"role": current_role, "content": content})
        current_role = None
        current_parts = []

    for line in text.splitlines():
        match = _ROLE_LINE_RE.match(line.strip())
        if match:
            flush()
            label = match.group(1).lower()
            if label in {"你", "用户"}:
                current_role = "user"
            elif label in {"助手"}:
                current_role = "assistant"
            else:
                current_role = _ROLE_ALIASES.get(label)
            remainder = match.group(2) or ""
            current_parts = [remainder] if remainder.strip() else []
            continue
        if current_role is not None:
            current_parts.append(line)
    flush()
    return _pair_messages(messages)
