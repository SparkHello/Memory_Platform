"""一次性 console 登录 code 的 REST 端点。

- ``POST /auth/console-login-code``：需 console role Bearer（EarlyAuthMiddleware
  按 /auth 前缀强制），返回一次性 code，明文只出现这一次。
- ``POST /auth/console-login-exchange``：免 Bearer，由 EarlyAuthMiddleware
  限定本机来源并套用速率限制；成功时交付 mint 时换发的 console token 明文，
  有且仅有一次。无效/过期/已使用一律 401 且响应体完全一致。
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.auth_tokens import _disable_secret_caching
from app.api.deps import (
    get_auth_principal,
    get_console_login_code_store,
    require_api_key,
)
from app.auth.console_login import (
    CONSOLE_LOGIN_CODE_TTL_SECONDS,
    ConsoleLoginCodeStore,
)
from app.auth.tokens import AuthPrincipal, AuthStoreError


router = APIRouter(prefix="/auth", tags=["console-login"])

# 所有交换失败共用同一状态码与响应体，不区分无效/过期/已使用。
_EXCHANGE_FAILURE_DETAIL = "登录 code 无效或已过期"


class ConsoleLoginCodeResponse(BaseModel):
    code: str
    token_id: str
    expires_at: str
    expires_in_seconds: int


class ConsoleLoginExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 不做长度/格式约束：任何不满足条件的 code 都在 store 层按统一 401 处理，
    # 避免 422 与 401 的差异泄露 code 格式信息。
    code: str


class ConsoleLoginExchangeResponse(BaseModel):
    token: str
    token_id: str
    user_id: str
    # 只有同进程宿主（安卓 App）为本机浏览器签发的 code 才会携带管理密钥；
    # 通过 HTTP 签发的 code 一律为 null。
    model_admin_key: str | None = None


@router.post(
    "/console-login-code",
    response_model=ConsoleLoginCodeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def mint_console_login_code(
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_auth_principal)],
    store: Annotated[ConsoleLoginCodeStore, Depends(get_console_login_code_store)],
) -> ConsoleLoginCodeResponse:
    _disable_secret_caching(response)
    try:
        minted = store.mint(user_id=principal.user_id)
    except (AuthStoreError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法生成登录 code，请重试",
        ) from exc
    return ConsoleLoginCodeResponse(
        code=minted.code,
        token_id=minted.token_id,
        expires_at=minted.expires_at,
        expires_in_seconds=CONSOLE_LOGIN_CODE_TTL_SECONDS,
    )


@router.post(
    "/console-login-exchange",
    response_model=ConsoleLoginExchangeResponse,
)
def exchange_console_login_code(
    body: ConsoleLoginExchangeRequest,
    response: Response,
    store: Annotated[ConsoleLoginCodeStore, Depends(get_console_login_code_store)],
) -> ConsoleLoginExchangeResponse:
    _disable_secret_caching(response)
    delivered = store.exchange(body.code)
    if delivered is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_EXCHANGE_FAILURE_DETAIL,
        )
    return ConsoleLoginExchangeResponse(
        token=delivered.token,
        token_id=delivered.token_id,
        user_id=delivered.user_id,
        model_admin_key=delivered.admin_key,
    )
