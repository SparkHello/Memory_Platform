from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from typing import Any


BUILTIN_PRICING_PATH = Path(__file__).parents[1] / "catalog" / "pricing.json"
_configured_pricing_path = ""


class PricingCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    key: str
    provider: str
    provider_label: str
    model: str
    kind: str
    currency: str
    input_cache_hit_per_million: Decimal
    input_cache_miss_per_million: Decimal
    output_per_million: Decimal
    source_url: str
    input_token_min: int = 0
    input_token_max: int | None = None
    input_range_label: str = ""
    as_of: str = ""

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        for field in (
            "input_cache_hit_per_million",
            "input_cache_miss_per_million",
            "output_per_million",
        ):
            data[field] = str(data[field])
        return data


def normalize_model_name(model: str) -> str:
    return model.strip().lower().rsplit("/", 1)[-1]


def provider_slug(
    *,
    provider_code: str = "",
    model: str = "",
    base_url: str = "",
) -> str:
    code = provider_code.strip().upper()
    model_name = normalize_model_name(model)
    target = f"{base_url} {model_name}".lower()
    if code == "M" or "xiaomimimo" in target or model_name.startswith("mimo-"):
        return "mimo"
    if code == "K" or "moonshot" in target or model_name.startswith("kimi-"):
        return "kimi"
    if "deepseek" in target or model_name.startswith("deepseek-"):
        return "deepseek"
    if (
        "bigmodel" in target
        or "zhipu" in target
        or model_name.startswith(("glm-", "embedding-"))
    ):
        return "zhipu"
    if "dashscope" in target or "aliyun" in target or model_name.startswith(
        ("text-embedding-", "qwen")
    ):
        return "alibaba"
    return "upstream" if code == "D" else "custom"


def provider_label(provider: str) -> str:
    return {
        "mimo": "MiMo",
        "kimi": "Kimi",
        "deepseek": "DeepSeek",
        "zhipu": "智谱",
        "alibaba": "阿里云百炼",
        "upstream": "兼容上游",
        "custom": "自定义上游",
    }.get(provider, provider or "未知")


def price_for(
    *,
    provider: str,
    model: str,
    kind: str,
    input_tokens: int | None = None,
) -> ModelPrice | None:
    normalized = normalize_model_name(model)
    prices, _ = load_pricing_catalog()
    candidates = [
        price
        for price in prices
        if (
            price.provider == provider
            and price.model == normalized
            and price.kind == kind
        )
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if input_tokens is None:
        return None
    token_count = max(0, int(input_tokens))
    return next(
        (
            price
            for price in candidates
            if token_count >= price.input_token_min
            and (
                price.input_token_max is None
                or token_count < price.input_token_max
            )
        ),
        None,
    )


def load_pricing_catalog(
    path: str | Path | None = None,
) -> tuple[tuple[ModelPrice, ...], dict[str, str]]:
    builtins, builtin_meta = _parse_catalog(_load_json(BUILTIN_PRICING_PATH))
    selected = str(
        path or _configured_pricing_path or os.getenv("PRICING_CATALOG_PATH", "")
    ).strip()
    if not selected:
        return tuple(builtins.values()), builtin_meta
    overlay_path = Path(selected).expanduser()
    if overlay_path.resolve() == BUILTIN_PRICING_PATH.resolve():
        return tuple(builtins.values()), builtin_meta
    overlay, overlay_meta = _parse_catalog(_load_json(overlay_path))
    merged = {**builtins, **overlay}
    return tuple(merged.values()), {
        "as_of": overlay_meta["as_of"],
        "currency": overlay_meta["currency"],
        "note": overlay_meta.get("note") or builtin_meta.get("note", ""),
    }


def pricing_catalog() -> dict[str, object]:
    prices, metadata = load_pricing_catalog()
    return {
        "as_of": metadata["as_of"],
        "currency": metadata["currency"],
        "models": [price.public_dict() for price in prices],
        "note": metadata["note"],
    }


def configure_pricing_catalog(path: str = "") -> None:
    global _configured_pricing_path
    selected = path.strip()
    if selected:
        load_pricing_catalog(selected)
    _configured_pricing_path = selected


def _parse_catalog(
    payload: dict[str, Any],
) -> tuple[dict[str, ModelPrice], dict[str, str]]:
    if payload.get("version") != 1:
        raise PricingCatalogError("价格目录 version 必须为 1")
    as_of = _required_string(payload.get("as_of"), "as_of")
    currency = _required_string(payload.get("currency"), "currency").upper()
    raw_prices = payload.get("models")
    if not isinstance(raw_prices, list):
        raise PricingCatalogError("价格目录缺少 models 数组")
    prices: dict[str, ModelPrice] = {}
    for raw in raw_prices:
        if not isinstance(raw, dict):
            raise PricingCatalogError("models 中的每一项都必须是对象")
        key = _required_string(raw.get("key"), "price key")
        if key in prices:
            raise PricingCatalogError(f"价格 key 重复：{key}")
        item_currency = str(raw.get("currency") or currency).strip().upper()
        source_url = _required_string(raw.get("source_url"), "source_url")
        if not source_url.startswith("https://"):
            raise PricingCatalogError(f"价格来源必须使用 HTTPS：{key}")
        token_min = _non_negative_int(raw.get("input_token_min", 0), "input_token_min")
        raw_max = raw.get("input_token_max")
        token_max = (
            None
            if raw_max is None
            else _non_negative_int(raw_max, "input_token_max")
        )
        if token_max is not None and token_max <= token_min:
            raise PricingCatalogError(f"价格分档上限必须大于下限：{key}")
        prices[key] = ModelPrice(
            key=key,
            provider=_required_string(raw.get("provider"), "provider").lower(),
            provider_label=_required_string(raw.get("provider_label"), "provider_label"),
            model=normalize_model_name(_required_string(raw.get("model"), "model")),
            kind=_required_string(raw.get("kind"), "kind").lower(),
            currency=item_currency,
            input_cache_hit_per_million=_non_negative_decimal(
                raw.get("input_cache_hit_per_million"),
                "input_cache_hit_per_million",
            ),
            input_cache_miss_per_million=_non_negative_decimal(
                raw.get("input_cache_miss_per_million"),
                "input_cache_miss_per_million",
            ),
            output_per_million=_non_negative_decimal(
                raw.get("output_per_million"),
                "output_per_million",
            ),
            source_url=source_url,
            input_token_min=token_min,
            input_token_max=token_max,
            input_range_label=str(raw.get("input_range_label") or "").strip(),
            as_of=str(raw.get("as_of") or as_of).strip(),
        )
    return prices, {
        "as_of": as_of,
        "currency": currency,
        "note": str(payload.get("note") or "").strip(),
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PricingCatalogError(f"价格目录不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise PricingCatalogError(f"价格目录不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PricingCatalogError(f"价格目录顶层必须是对象：{path}")
    return payload


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingCatalogError(f"{label} 必须是非空字符串")
    return value.strip()


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise PricingCatalogError(f"{label} 必须是非负整数")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PricingCatalogError(f"{label} 必须是非负整数") from exc
    if parsed < 0:
        raise PricingCatalogError(f"{label} 必须是非负整数")
    return parsed


def _non_negative_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingCatalogError(f"{label} 必须是非负数字") from exc
    if not parsed.is_finite() or parsed < 0:
        raise PricingCatalogError(f"{label} 必须是非负数字")
    return parsed
