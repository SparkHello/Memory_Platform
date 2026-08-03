from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


DEFAULT_MAX_EVENT_BUFFER_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_CAPTURE_TEXT_CHARS = 64 * 1024
DEFAULT_MAX_CAPTURE_REASONING_CHARS = 128 * 1024
DEFAULT_MAX_TOOL_CALL_IDS = 64
DEFAULT_MAX_TOOL_CALL_ID_BYTES = 16 * 1024


@dataclass(slots=True)
class ChatStreamCapture:
    """Sidecar parser for an SSE stream that is forwarded byte-for-byte.

    Parsing never changes downstream chunks. It only collects the final
    assistant text so memory ingestion can run after a complete response.
    """

    _buffer: bytearray = field(default_factory=bytearray)
    _text_parts: list[str] = field(default_factory=list)
    _reasoning_parts: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)
    _tool_call_id_set: set[str] = field(default_factory=set)
    finish_reason: str | None = None
    saw_tool_calls: bool = False
    saw_error: bool = False
    saw_done: bool = False
    usage: dict[str, Any] | None = None
    response_id: str = ""
    response_model: str = ""
    stream_ended_cleanly: bool = False
    capture_overflowed: bool = False
    max_event_buffer_bytes: int = DEFAULT_MAX_EVENT_BUFFER_BYTES
    max_text_chars: int = DEFAULT_MAX_CAPTURE_TEXT_CHARS
    max_reasoning_chars: int = DEFAULT_MAX_CAPTURE_REASONING_CHARS
    max_tool_call_ids: int = DEFAULT_MAX_TOOL_CALL_IDS
    max_tool_call_id_bytes: int = DEFAULT_MAX_TOOL_CALL_ID_BYTES
    _text_length: int = 0
    _reasoning_length: int = 0
    _tool_call_id_bytes: int = 0

    def feed(self, chunk: bytes) -> None:
        if (
            not chunk
            or self.capture_overflowed
            or self.saw_error
            or self.saw_done
        ):
            return
        self._buffer.extend(chunk)
        while True:
            if self.saw_done or self.saw_error:
                self._buffer.clear()
                return
            event = _pop_sse_event(self._buffer)
            if event is None:
                break
            if len(event) > self.max_event_buffer_bytes:
                self._disable_capture()
                return
            self._consume_event(event)
            if self.capture_overflowed:
                return
        if len(self._buffer) > self.max_event_buffer_bytes:
            self._disable_capture()

    def finish(self, *, clean: bool) -> None:
        if clean and self._buffer and not self.capture_overflowed:
            self._consume_event(bytes(self._buffer))
            self._buffer.clear()
        self.stream_ended_cleanly = clean

    @property
    def assistant_text(self) -> str:
        return "".join(self._text_parts).strip()

    @property
    def assistant_reasoning(self) -> str:
        return "".join(self._reasoning_parts)

    @property
    def is_final_text_response(self) -> bool:
        return self.stream_ended_cleanly and self.final_text_trace_ready

    @property
    def final_text_trace_ready(self) -> bool:
        if self.capture_overflowed or self.saw_error or not self.saw_done:
            return False
        if self.saw_tool_calls:
            return False
        if self.finish_reason in {
            "tool_calls",
            "function_call",
            "content_filter",
            "length",
        }:
            return False
        return bool(self.assistant_text)

    @property
    def is_complete_tool_call_response(self) -> bool:
        return self.stream_ended_cleanly and self.tool_call_trace_ready

    @property
    def tool_call_trace_ready(self) -> bool:
        """Whether a complete SSE tool trace has reached its `[DONE]` marker."""
        if self.capture_overflowed or self.saw_error or not self.saw_done:
            return False
        if not self.saw_tool_calls or not self.tool_call_ids:
            return False
        return self.finish_reason not in {"content_filter", "length"}

    def _consume_event(self, event: bytes) -> None:
        data_lines: list[bytes] = []
        for line in event.splitlines():
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return
        # Match FLIT's parser: each data line is an independent JSON payload,
        # and a provider may put a final JSON chunk plus [DONE] in one event.
        for raw_data in data_lines:
            raw_data = raw_data.strip()
            if not raw_data:
                continue
            if raw_data == b"[DONE]":
                self.saw_done = True
                return
            try:
                payload = json.loads(raw_data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.saw_error = True
                return
            if not isinstance(payload, dict):
                self.saw_error = True
                return
            self._consume_payload(payload)
            if self.capture_overflowed or self.saw_error:
                return

    def _consume_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("error") is not None:
            self.saw_error = True
            return
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = dict(usage)
        response_id = payload.get("id")
        if isinstance(response_id, str) and response_id:
            self.response_id = response_id
        response_model = payload.get("model")
        if isinstance(response_model, str) and response_model:
            self.response_model = response_model
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0]
        if not isinstance(choice, dict):
            return
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str):
            self.finish_reason = finish_reason

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            message = choice.get("message")
            delta = message if isinstance(message, dict) else {}
        if isinstance(delta.get("tool_calls"), list) and delta["tool_calls"]:
            self.saw_tool_calls = True
            for tool_call in delta["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = tool_call.get("id")
                if (
                    isinstance(tool_call_id, str)
                    and tool_call_id
                    and tool_call_id not in self._tool_call_id_set
                ):
                    tool_call_id_bytes = len(tool_call_id.encode("utf-8"))
                    if (
                        len(self.tool_call_ids) >= self.max_tool_call_ids
                        or self._tool_call_id_bytes + tool_call_id_bytes
                        > self.max_tool_call_id_bytes
                    ):
                        self._disable_capture()
                        return
                    self.tool_call_ids.append(tool_call_id)
                    self._tool_call_id_set.add(tool_call_id)
                    self._tool_call_id_bytes += tool_call_id_bytes
        if delta.get("function_call"):
            self.saw_tool_calls = True
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, str):
            if self._reasoning_length + len(reasoning) > self.max_reasoning_chars:
                self._disable_capture()
                return
            self._reasoning_parts.append(reasoning)
            self._reasoning_length += len(reasoning)
        text_values = _text_values(delta.get("content"))
        added_length = sum(len(value) for value in text_values)
        if self._text_length + added_length > self.max_text_chars:
            self._disable_capture()
            return
        self._text_parts.extend(text_values)
        self._text_length += added_length

    def _disable_capture(self) -> None:
        self.capture_overflowed = True
        self._buffer.clear()
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self.tool_call_ids.clear()
        self._tool_call_id_set.clear()
        self._text_length = 0
        self._reasoning_length = 0
        self._tool_call_id_bytes = 0


def _pop_sse_event(buffer: bytearray) -> bytes | None:
    candidates = [
        (buffer.find(b"\n\n"), 2),
        (buffer.find(b"\r\n\r\n"), 4),
    ]
    available = [(index, size) for index, size in candidates if index >= 0]
    if not available:
        return None
    index, delimiter_size = min(available, key=lambda item: item[0])
    event = bytes(buffer[:index])
    del buffer[: index + delimiter_size]
    return event


def extract_non_stream_result(payload: dict[str, Any]) -> tuple[str, bool]:
    """Return (assistant_text, is_final_text_response) for choice zero."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", False
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", False
    message = choice.get("message")
    if not isinstance(message, dict):
        return "", False
    if message.get("tool_calls") or message.get("function_call"):
        return "", False
    finish_reason = choice.get("finish_reason")
    if finish_reason in {"tool_calls", "function_call", "content_filter", "length"}:
        return "", False
    text = "".join(_text_values(message.get("content"))).strip()
    if len(text) > DEFAULT_MAX_CAPTURE_TEXT_CHARS:
        return "", False
    return text, bool(text)


def extract_non_stream_tool_trace(
    payload: dict[str, Any],
) -> tuple[str, list[str], bool]:
    """Return reasoning, tool-call IDs, and whether the tool response is complete."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", [], False
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", [], False
    message = choice.get("message")
    if not isinstance(message, dict):
        return "", [], False
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return "", [], False
    tool_call_ids = [
        tool_call["id"]
        for tool_call in tool_calls
        if isinstance(tool_call, dict)
        and isinstance(tool_call.get("id"), str)
        and tool_call["id"]
    ]
    if not tool_call_ids:
        return "", [], False
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = message.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    if len(reasoning) > DEFAULT_MAX_CAPTURE_REASONING_CHARS:
        return "", [], False
    finish_reason = choice.get("finish_reason")
    return (
        reasoning,
        list(dict.fromkeys(tool_call_ids)),
        finish_reason not in {"content_filter", "length"},
    )


def extract_non_stream_reasoning(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = message.get("reasoning")
    if not isinstance(reasoning, str):
        return ""
    if len(reasoning) > DEFAULT_MAX_CAPTURE_REASONING_CHARS:
        return ""
    return reasoning


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    text_parts: list[str] = []
    for part in value:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type not in {"text", "output_text"}:
            continue
        text = part.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return text_parts
