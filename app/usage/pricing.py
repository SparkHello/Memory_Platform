from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal


PRICING_AS_OF = "2026-07-31"


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
    as_of: str = PRICING_AS_OF

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        for field in (
            "input_cache_hit_per_million",
            "input_cache_miss_per_million",
            "output_per_million",
        ):
            data[field] = str(data[field])
        return data


_PRICES = (
    ModelPrice(
        key="mimo:mimo-v2.5-pro-ultraspeed",
        provider="mimo",
        provider_label="MiMo",
        model="mimo-v2.5-pro-ultraspeed",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.075"),
        input_cache_miss_per_million=Decimal("9"),
        output_per_million=Decimal("18"),
        source_url="https://mimo.mi.com/models/en-US/mimo-v2.5-pro-ultraspeed",
    ),
    ModelPrice(
        key="kimi:kimi-k2.7-code",
        provider="kimi",
        provider_label="Kimi",
        model="kimi-k2.7-code",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("1.30"),
        input_cache_miss_per_million=Decimal("6.50"),
        output_per_million=Decimal("27"),
        source_url="https://platform.kimi.com/docs/pricing/chat-k27-code",
    ),
    ModelPrice(
        key="kimi:kimi-k2.7-code-highspeed",
        provider="kimi",
        provider_label="Kimi",
        model="kimi-k2.7-code-highspeed",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("2.60"),
        input_cache_miss_per_million=Decimal("13.00"),
        output_per_million=Decimal("54.00"),
        source_url="https://platform.kimi.com/docs/pricing/chat-k27-code",
    ),
    ModelPrice(
        key="deepseek:deepseek-v4-flash",
        provider="deepseek",
        provider_label="DeepSeek",
        model="deepseek-v4-flash",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.02"),
        input_cache_miss_per_million=Decimal("1"),
        output_per_million=Decimal("2"),
        source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    ),
    ModelPrice(
        key="deepseek:deepseek-v4-pro",
        provider="deepseek",
        provider_label="DeepSeek",
        model="deepseek-v4-pro",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.025"),
        input_cache_miss_per_million=Decimal("3"),
        output_per_million=Decimal("6"),
        source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    ),
    ModelPrice(
        key="zhipu:glm-5.1:input-lt-32k",
        provider="zhipu",
        provider_label="智谱",
        model="glm-5.1",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("1.3"),
        input_cache_miss_per_million=Decimal("6"),
        output_per_million=Decimal("24"),
        source_url="https://bigmodel.cn/pricing",
        input_token_max=32_000,
        input_range_label="输入 < 32K Token",
    ),
    ModelPrice(
        key="zhipu:glm-5.1:input-gte-32k",
        provider="zhipu",
        provider_label="智谱",
        model="glm-5.1",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("2"),
        input_cache_miss_per_million=Decimal("8"),
        output_per_million=Decimal("28"),
        source_url="https://bigmodel.cn/pricing",
        input_token_min=32_000,
        input_range_label="输入 ≥ 32K Token",
    ),
    ModelPrice(
        key="zhipu:glm-5.2",
        provider="zhipu",
        provider_label="智谱",
        model="glm-5.2",
        kind="chat",
        currency="CNY",
        input_cache_hit_per_million=Decimal("2"),
        input_cache_miss_per_million=Decimal("8"),
        output_per_million=Decimal("28"),
        source_url="https://bigmodel.cn/pricing",
    ),
    ModelPrice(
        key="alibaba:text-embedding-v4",
        provider="alibaba",
        provider_label="阿里云百炼",
        model="text-embedding-v4",
        kind="embedding",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.5"),
        input_cache_miss_per_million=Decimal("0.5"),
        output_per_million=Decimal("0"),
        source_url="https://help.aliyun.com/zh/model-studio/text-embedding-v4",
    ),
    ModelPrice(
        key="alibaba:qwen3.7-text-embedding",
        provider="alibaba",
        provider_label="阿里云百炼",
        model="qwen3.7-text-embedding",
        kind="embedding",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.5"),
        input_cache_miss_per_million=Decimal("0.5"),
        output_per_million=Decimal("0"),
        source_url="https://help.aliyun.com/zh/model-studio/qwen3-7-text-embedding",
    ),
    ModelPrice(
        key="zhipu:embedding-3",
        provider="zhipu",
        provider_label="智谱",
        model="embedding-3",
        kind="embedding",
        currency="CNY",
        input_cache_hit_per_million=Decimal("0.5"),
        input_cache_miss_per_million=Decimal("0.5"),
        output_per_million=Decimal("0"),
        source_url="https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3",
    ),
)


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
    candidates = [
        price
        for price in _PRICES
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


def pricing_catalog() -> dict[str, object]:
    return {
        "as_of": PRICING_AS_OF,
        "currency": "CNY",
        "models": [price.public_dict() for price in _PRICES],
        "note": (
            "金额按事件发生时保存的公开 API 原价快照计算，不含套餐、赠金、"
            "限时折扣或账户级优惠；未匹配到公开单价的模型不会计入金额。"
        ),
    }
