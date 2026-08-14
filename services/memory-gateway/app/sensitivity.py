"""Single source of truth for the local, deterministic content-sensitivity floor.

This module is intentionally neutral: it must not import app.memory.* or
app.knowledge.*, so both the memory store and the physically-isolated
knowledge store can share one vocabulary without violating their isolation.

The pattern tables below are the union of the two formerly divergent copies
(app.memory.redaction and app.knowledge.store).  The union only makes
detection more conservative; it never widens the "normal" floor.
"""

from __future__ import annotations

import re

SensitivityLevel = str

SENSITIVITY_RANK: dict[str, int] = {"normal": 0, "private": 1, "sensitive": 2}

# These patterns intentionally require either a high-risk context word or a
# recognizable identifier shape. They are a local safety floor, not a general
# purpose PII classifier.
SENSITIVE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "credential": (
        r"密码",
        r"口令",
        r"验证码",
        r"密钥",
        r"私钥",
        r"助记词",
        r"\bpass(?:word|code)\b",
        r"\bpasswd\b",
        r"\bpin\s*(?:code)?\b",
        r"\botp\b",
        r"\bapi[-_ ]?key\b",
        r"\baccess[-_ ]?token\b",
        r"\brefresh[-_ ]?token\b",
        r"\bsecret[-_ ]?key\b",
        r"\bprivate[-_ ]?key\b",
        r"\bseed phrase\b",
        r"\b(?:sk|pk|token)[-_][A-Za-z0-9_-]{4,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{16,}\b",
        r"\bAKIA[A-Z0-9]{16}\b",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)\b\s*[:=]",
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
        r"(?<!\d)\d{15,19}(?!\d)",
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

PRIVATE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "contact": (
        r"手机号",
        r"电话号码",
        r"电子邮箱",
        r"邮箱地址",
        r"\bphone number\b",
        r"\be-?mail address\b",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
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
    """Return (sensitive_categories, private_categories) for text."""
    sensitive = {
        category
        for category, patterns in SENSITIVE_CATEGORY_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }
    private = {
        category
        for category, patterns in PRIVATE_CATEGORY_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }
    return sensitive, private


def detect_text_sensitivity(text: str) -> SensitivityLevel:
    """Return the deterministic local sensitivity floor for arbitrary text."""
    sensitive_categories, private_categories = detected_sensitive_categories(text)
    if sensitive_categories:
        return "sensitive"
    if private_categories:
        return "private"
    return "normal"
