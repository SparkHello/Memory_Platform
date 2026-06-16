from fastapi import APIRouter, status


router = APIRouter(
    prefix="/v1",
    tags=["deprecated"],
)

DEPRECATED_V1_MESSAGE = (
    "The OpenAI-compatible /v1 gateway has been removed. "
    "Use the MCP endpoint at /mcp or the memory management endpoints under /memories."
)


def _deprecated_v1_response() -> dict:
    return {
        "error": {
            "code": "v1_gateway_deprecated",
            "message": DEPRECATED_V1_MESSAGE,
        }
    }


@router.get("/models", status_code=status.HTTP_410_GONE)
def deprecated_models() -> dict:
    return _deprecated_v1_response()


@router.post("/chat/completions", status_code=status.HTTP_410_GONE)
async def deprecated_chat_completions() -> dict:
    return _deprecated_v1_response()
