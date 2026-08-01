from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: str = Field(min_length=1)
    # External OpenAI-compatible clients may send a plain string, null, or
    # multimodal parts such as text/image_url/input_audio. Keep this open so
    # the transparent chat gateway does not destroy client-specific payloads.
    content: Any = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    response_format: dict[str, Any] | None = None
    stream: bool = False
    user: str | None = None
    conversation_id: str | None = None

    model_config = ConfigDict(extra="allow")

    def extra_payload(self) -> dict[str, Any]:
        return self.model_extra or {}
