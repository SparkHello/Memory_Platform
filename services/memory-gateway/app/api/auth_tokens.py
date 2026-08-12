from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    get_auth_principal,
    get_auth_token_store,
    require_api_key,
)
from app.auth.tokens import (
    AuthPrincipal,
    AuthRole,
    AuthStoreError,
    AuthTokenRecord,
    AuthTokenStore,
    LastActiveConsoleTokenError,
    MemoryAccess,
)
from app.config import Settings, get_settings


router = APIRouter(
    prefix="/auth/tokens",
    tags=["auth-tokens"],
    dependencies=[Depends(require_api_key)],
)


class AuthTokenView(BaseModel):
    token_id: str
    name: str
    user_id: str
    role: AuthRole
    memory_access: MemoryAccess = "read-write"
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    is_current: bool = False
    can_revoke: bool = True
    revoke_block_reason: Literal["last_active_console_token"] | None = None


class AuthTokenListResponse(BaseModel):
    data: list[AuthTokenView]
    current_user_id: str
    legacy_key_enabled: bool
    authenticated_with_legacy_key: bool
    allowed_create_roles: list[Literal["chat", "mcp"]] = Field(
        default_factory=lambda: ["chat", "mcp"]
    )


class AuthTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    role: Literal["chat", "mcp"]
    # Only meaningful for chat tokens: read = proxy + recall, no auto extract.
    memory_access: MemoryAccess = "read-write"


class AuthTokenCreateResponse(BaseModel):
    token: str
    record: AuthTokenView


class AuthTokenRevokeResponse(BaseModel):
    revoked: bool
    already_revoked: bool
    record: AuthTokenView


@router.get("", response_model=AuthTokenListResponse)
def list_auth_tokens(
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_auth_principal)],
    store: Annotated[AuthTokenStore, Depends(get_auth_token_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthTokenListResponse:
    _disable_secret_caching(response)
    records = store.list_tokens(user_id=principal.user_id)
    active_console_count = _active_console_count(records)
    return AuthTokenListResponse(
        data=[
            _token_view(
                record,
                principal=principal,
                active_console_count=active_console_count,
            )
            for record in records
        ],
        current_user_id=principal.user_id,
        legacy_key_enabled=bool(
            settings.gateway_legacy_api_key_enabled and settings.gateway_api_key
        ),
        authenticated_with_legacy_key=principal.legacy,
    )


@router.post(
    "",
    response_model=AuthTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_auth_token(
    body: AuthTokenCreateRequest,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_auth_principal)],
    store: Annotated[AuthTokenStore, Depends(get_auth_token_store)],
) -> AuthTokenCreateResponse:
    _disable_secret_caching(response)
    # The public REST surface deliberately cannot mint another console token.
    # Console credentials remain a local CLI/bootstrap responsibility.
    if body.role not in {"chat", "mcp"}:  # pragma: no cover - Literal is first line
        raise HTTPException(status_code=422, detail="REST 只能创建 chat 或 mcp token")
    try:
        created = store.create_token(
            name=body.name,
            user_id=principal.user_id,
            role=body.role,
            memory_access=body.memory_access,
        )
    except AuthStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AuthTokenCreateResponse(
        token=created.token,
        record=_token_view(
            created.record,
            principal=principal,
            active_console_count=_active_console_count(
                store.list_tokens(user_id=principal.user_id)
            ),
        ),
    )


@router.delete("/{token_id}", response_model=AuthTokenRevokeResponse)
def revoke_auth_token(
    token_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")],
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_auth_principal)],
    store: Annotated[AuthTokenStore, Depends(get_auth_token_store)],
) -> AuthTokenRevokeResponse:
    _disable_secret_caching(response)
    existing = next(
        (
            record
            for record in store.list_tokens(user_id=principal.user_id)
            if record.token_id == token_id
        ),
        None,
    )
    if existing is None:
        # Do not reveal whether the identifier belongs to another user.
        raise HTTPException(status_code=404, detail="token 不存在")
    already_revoked = existing.revoked_at is not None
    if not already_revoked:
        try:
            revoked = store.revoke_token(
                token_id,
                user_id=principal.user_id,
                protect_last_console=True,
            )
        except LastActiveConsoleTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "last_active_console_token",
                    "message": str(exc),
                },
            ) from exc
        except AuthStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not revoked:
            # A concurrent request may have revoked it after the read above.
            already_revoked = True
    records = store.list_tokens(user_id=principal.user_id)
    current = next(record for record in records if record.token_id == token_id)
    return AuthTokenRevokeResponse(
        revoked=True,
        already_revoked=already_revoked,
        record=_token_view(
            current,
            principal=principal,
            active_console_count=_active_console_count(records),
        ),
    )


def _token_view(
    record: AuthTokenRecord,
    *,
    principal: AuthPrincipal,
    active_console_count: int,
) -> AuthTokenView:
    last_active_console = (
        record.role == "console"
        and record.revoked_at is None
        and active_console_count <= 1
    )
    return AuthTokenView(
        token_id=record.token_id,
        name=record.name,
        user_id=record.user_id,
        role=record.role,
        memory_access=record.memory_access,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
        is_current=(not principal.legacy and principal.token_id == record.token_id),
        can_revoke=not last_active_console,
        revoke_block_reason=(
            "last_active_console_token" if last_active_console else None
        ),
    )


def _active_console_count(records: list[AuthTokenRecord]) -> int:
    return sum(
        record.role == "console" and record.revoked_at is None
        for record in records
    )


def _disable_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
