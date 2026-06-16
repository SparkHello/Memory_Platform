import json
import logging

from fastapi import HTTPException, status
import httpx

from app.config import Settings
from app.openai_compat.schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        messages: list[dict[str, str]],
    ) -> dict:
        if not self.settings.upstream_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="UPSTREAM_API_KEY 未配置",
            )

        payload = request.model_dump(exclude_none=True, exclude={"conversation_id"})
        payload["model"] = self.settings.upstream_model or request.model
        payload["messages"] = messages
        payload["stream"] = False

        if _should_enable_zhipu_thinking(
            base_url=self.settings.upstream_base_url,
            upstream_model=payload["model"],
        ):
            payload["thinking"] = {"type": "enabled"}

        url = f"{self.settings.upstream_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.upstream_api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _safe_error_detail(exc.response)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"上游模型 API 返回错误：{detail}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"调用上游模型 API 失败：{exc}",
            ) from exc

        try:
            return _json_from_utf8_bytes(response)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="上游模型 API 返回了无法解析的 JSON",
            ) from exc


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        data = _json_from_utf8_bytes(response)
    except (UnicodeError, json.JSONDecodeError):
        return response.content.decode("utf-8", errors="replace")[:500]
    return str(data)[:500]


def _json_from_utf8_bytes(response: httpx.Response) -> dict:
    try:
        raw_text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("上游响应不是合法 UTF-8，已使用替换字符解码。")
        raw_text = response.content.decode("utf-8", errors="replace")
    return json.loads(raw_text)


def _should_enable_zhipu_thinking(
    *,
    base_url: str = "",
    upstream_model: str = "",
) -> bool:
    provider_text = base_url.lower()
    model = upstream_model.lower()
    is_zhipu = any(marker in provider_text for marker in ("bigmodel", "z.ai", "zhipu"))
    thinking_model = model.startswith(("glm-5", "glm-4.7", "glm-4.6", "glm-4.5"))
    return is_zhipu and thinking_model
