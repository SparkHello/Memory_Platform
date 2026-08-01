from collections.abc import Mapping
import re
from typing import Any

from app.memory.models import MemorySensitivity


SENSITIVE_LEVELS = {"private", "sensitive"}

REDACTED_CONTENT_TEXT = "内容已遮罩。请在详情页显式查看完整内容。"
REDACTED_SOURCE_TEXT = "来源原文已遮罩。请在详情页显式查看完整内容。"


_SENSITIVITY_RANK = {"normal": 0, "private": 1, "sensitive": 2}

# These patterns intentionally require either a high-risk context word or a
# recognizable identifier shape. They are a local safety floor, not a general
# purpose PII classifier.
_SENSITIVE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "credential": (
        r"密码",
        r"口令",
        r"验证码",
        r"密钥",
        r"私钥",
        r"助记词",
        r"\bpass(?:word|code)\b",
        r"\bpin\s*(?:code)?\b",
        r"\botp\b",
        r"\bapi[-_ ]?key\b",
        r"\baccess[-_ ]?token\b",
        r"\bsecret[-_ ]?key\b",
        r"\bprivate[-_ ]?key\b",
        r"\bseed phrase\b",
        r"\b(?:sk|pk|token)[-_][A-Za-z0-9_-]{4,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{16,}\b",
        r"\bAKIA[A-Z0-9]{16}\b",
    ),
    "government_id": (
        r"身份证",
        r"护照号",
        r"社保号",
        r"驾驶证号",
        r"\bpassport (?:number|no\.?|id)\b",
        r"\bsocial security\b",
        r"\bssn\b",
        r"(?<!\d)\d{17}[\dXx](?!\d)",
    ),
    "health": (
        r"健康隐私",
        r"病历",
        r"确诊",
        r"诊断",
        r"疾病",
        r"患有",
        r"过敏",
        r"用药",
        r"药物",
        r"处方",
        r"病史",
        r"症状",
        r"治疗",
        r"手术",
        r"血糖",
        r"血压",
        r"心率",
        r"糖尿病",
        r"癌症",
        r"抑郁症",
        r"焦虑症",
        r"\bmedical\b",
        r"\bdiagnos(?:is|ed)\b",
        r"\bdisease\b",
        r"\ballerg(?:y|ic)\b",
        r"\bmedication\b",
        r"\bprescription\b",
    ),
    "financial_account": (
        r"银行卡",
        r"信用卡",
        r"银行账户",
        r"银行账号",
        r"支付账号",
        r"账户余额",
        r"\bcredit card\b",
        r"\bdebit card\b",
        r"\bbank account\b",
        r"\baccount balance\b",
        r"(?<!\d)(?:\d[\s-]?){13,19}(?!\d)",
    ),
    "precise_address": (
        r"家庭住址",
        r"家庭地址",
        r"详细地址",
        r"门牌号",
        r"收货地址",
        r"\bhome address\b",
        r"\bstreet address\b",
        r"(?:省|市|区|县).{0,20}(?:路|街|道|巷|弄).{0,10}\d+\s*号",
        r"\b\d{1,6}\s+[A-Za-z][A-Za-z .'-]{1,40}\s+(?:Street|St|Road|Rd|Avenue|Ave)\b",
    ),
}

_PRIVATE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "contact": (
        r"手机号",
        r"电话号码",
        r"电子邮箱",
        r"邮箱地址",
        r"\bphone number\b",
        r"\be-?mail address\b",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
    ),
    "private_finance": (
        r"工资",
        r"收入",
        r"债务",
        r"负债",
        r"\bsalary\b",
        r"\bincome\b",
        r"\bdebt\b",
    ),
}


def detected_sensitive_categories(text: str) -> tuple[set[str], set[str]]:
    sensitive = {
        category
        for category, patterns in _SENSITIVE_CATEGORY_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }
    private = {
        category
        for category, patterns in _PRIVATE_CATEGORY_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }
    return sensitive, private


def detect_text_sensitivity(text: str) -> MemorySensitivity:
    """Return the deterministic local sensitivity floor for arbitrary text."""
    sensitive_categories, private_categories = detected_sensitive_categories(text)
    if sensitive_categories:
        return "sensitive"
    if private_categories:
        return "private"
    return "normal"


def sensitivity_floor(
    declared: MemorySensitivity,
    *texts: str | None,
) -> MemorySensitivity:
    """Raise a declared sensitivity to the deterministic local floor."""
    detected = detect_text_sensitivity("\n".join(text for text in texts if text))
    return max((declared, detected), key=_SENSITIVITY_RANK.__getitem__)


def redact_memory_payload(
    payload: Mapping[str, Any],
    *,
    redact_sensitive: bool,
    sensitivity: str | None = None,
) -> dict[str, Any]:
    data = dict(payload)
    if not redact_sensitive:
        return data

    declared_reason = sensitivity or data.get("sensitivity")
    declared_level: MemorySensitivity = (
        declared_reason if declared_reason in _SENSITIVITY_RANK else "normal"
    )
    local_text = "\n".join(
        str(data.get(field_name) or "")
        for field_name in ("content", "source_message", "source_excerpt")
    )
    reason = sensitivity_floor(declared_level, local_text)
    if reason not in SENSITIVE_LEVELS:
        return data

    redacted_fields: list[str] = []
    if "content" in data:
        data["content"] = REDACTED_CONTENT_TEXT
        redacted_fields.append("content")
    if data.get("source_message"):
        data["source_message"] = REDACTED_SOURCE_TEXT
        redacted_fields.append("source_message")
    if data.get("source_excerpt"):
        data["source_excerpt"] = REDACTED_SOURCE_TEXT
        redacted_fields.append("source_excerpt")
    if data.get("label"):
        data["label"] = "敏感记忆" if reason == "sensitive" else "私密记忆"
        redacted_fields.append("label")
    if data.get("entities"):
        data["entities"] = []
        redacted_fields.append("entities")

    data["redacted"] = True
    data["redaction_reason"] = reason
    data["redacted_fields"] = redacted_fields
    return data
