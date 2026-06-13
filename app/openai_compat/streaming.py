from fastapi import HTTPException, status


def raise_streaming_not_implemented() -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="stream=true 暂未实现，请使用非流式请求",
    )

