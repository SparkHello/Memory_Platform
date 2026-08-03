from __future__ import annotations

from dataclasses import dataclass
import hmac

from model_gateway.models import ClientConfig, GatewayConfig


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedClient:
    id: str
    config: ClientConfig


def authenticate_client(
    authorization: str,
    *,
    config: GatewayConfig,
    secrets: dict[str, str],
) -> AuthenticatedClient:
    scheme, separator, token = authorization.strip().partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("需要 Bearer 客户端密钥")
    presented = token.strip()
    matches: list[AuthenticatedClient] = []
    for client_id, client in config.clients.items():
        if not client.enabled:
            continue
        expected = secrets.get(client.secret_ref, "")
        if expected and hmac.compare_digest(presented, expected):
            matches.append(AuthenticatedClient(id=client_id, config=client))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AuthenticationError("客户端密钥配置冲突；每个 client 必须使用不同密钥")
    raise AuthenticationError("客户端密钥无效")
