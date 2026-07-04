from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    stream: bool = False
    user: str | None = None
    conversation_id: str | None = None

    model_config = ConfigDict(extra="allow")

    def extra_payload(self) -> dict[str, Any]:
        return self.model_extra or {}
