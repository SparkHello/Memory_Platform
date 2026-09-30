"""Single source of truth for the local, deterministic content-sensitivity floor.

This module is intentionally neutral: it must not import app.memory.* or
app.knowledge.*, so both the memory store and the physically-isolated
knowledge store can share one vocabulary without violating their isolation.

The pattern tables below are the union of the two formerly divergent copies
(app.memory.redaction and app.knowledge.store).  The union only makes
detection more conservative; it never widens the "normal" floor.

Two tiers with distinct semantics:

* ``sensitive`` -- secrets, identifiers and account numbers (credential,
  government_id, financial_account).  Never injected into chat, never sent to a
  remote extraction/embedding model unless ``ALLOW_SENSITIVE_EGRESS=true``, and
  only saved as a memory when the user explicitly asks to remember it.
* ``private`` -- personal-but-useful facts (health, precise_address, contact,
  private_finance).  Stored automatically, masked in console views by default,
  and recallable in ``/v1`` chat only when relevant to the current question.
"""

from __future__ import annotations

import re

SensitivityLevel = str

SENSITIVITY_RANK: dict[str, int] = {"normal": 0, "private": 1, "sensitive": 2}

# 邮箱形状的唯一定义：contact 类别检测与 extractor 结构化值扫描共用，
# extractor 不得再维护本地副本。
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
    re.IGNORECASE,
)

# These patterns intentionally require either a high-risk context word or a
# recognizable identifier shape. They are a local safety floor, not a general
# purpose PII classifier.
# Bare credential words.  A short memory sentence such as "我的密码是 x" is
# almost always about a real secret, so memory keeps treating the mention alone
# as sensitive.  Long imported documents (manuals, FAQs, tutorials) mention
# passwords constantly without containing one, so the knowledge detector only
# counts these words when an assignment marker and a value follow them.
CREDENTIAL_MENTION_PATTERNS: tuple[str, ...] = (
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
)

# The same words followed by an assignment marker and a value-like token.
CREDENTIAL_VALUE_PATTERNS: tuple[str, ...] = (
    r"(?:密码|口令|验证码|密钥|私钥|助记词)\s*(?:是|为|[:：=＝])\s*"
    r"(?=[^\s，。；、]*[A-Za-z0-9])[^\s，。；、]{4,}",
    r"(?:密码|口令|验证码|密钥|私钥)\s*[:：=＝]?\s*[A-Za-z0-9!@#$%^&*_+-]{6,}",
    r"\b(?:pass(?:word|code)|passwd|pin(?:\s*code)?|otp|api[-_ ]?key|access[-_ ]?token"
    r"|refresh[-_ ]?token|secret[-_ ]?key|private[-_ ]?key|seed phrase)\b\s*[:=：]\s*\S{4,}",
)

# Recognisable secret shapes count everywhere, with or without a keyword.
CREDENTIAL_SHAPE_PATTERNS: tuple[str, ...] = (
    r"\b(?:sk|pk|token)[-_][A-Za-z0-9_-]{4,}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{16,}\b",
    r"\bAKIA[A-Z0-9]{16}\b",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)\b\s*[:=]",
)

SENSITIVE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "credential": CREDENTIAL_MENTION_PATTERNS + CREDENTIAL_SHAPE_PATTERNS,
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
}

# Contact words that only *mention* a phone number or e-mail address; the
# shape patterns below still recognise actual numbers and addresses.
CONTACT_MENTION_PATTERNS: tuple[str, ...] = (
    r"手机号",
    r"电话号码",
    r"电子邮箱",
    r"邮箱地址",
    r"\bphone number\b",
    r"\be-?mail address\b",
)

PRIVATE_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
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
    "contact": CONTACT_MENTION_PATTERNS + (
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        EMAIL_PATTERN.pattern,
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


def detected_sensitive_categories(
    text: str,
    *,
    credential_requires_value: bool = False,
    contact_requires_value: bool = False,
) -> tuple[set[str], set[str]]:
    """Return (sensitive_categories, private_categories) for text.

    ``credential_requires_value`` switches the credential category from
    "mentions a password" to "contains something that looks like a password":
    long knowledge documents use it so a manual explaining how to reset a
    password is not filed as a secret.
    """
    sensitive = set()
    for category, patterns in SENSITIVE_CATEGORY_PATTERNS.items():
        if category == "credential" and credential_requires_value:
            patterns = CREDENTIAL_VALUE_PATTERNS + CREDENTIAL_SHAPE_PATTERNS
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            sensitive.add(category)
    private = set()
    for category, patterns in PRIVATE_CATEGORY_PATTERNS.items():
        if category == "contact" and contact_requires_value:
            patterns = tuple(
                pattern for pattern in patterns if pattern not in CONTACT_MENTION_PATTERNS
            )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            private.add(category)
    return sensitive, private


def detect_text_sensitivity(
    text: str,
    *,
    credential_requires_value: bool = False,
    contact_requires_value: bool = False,
) -> SensitivityLevel:
    """Return the deterministic local sensitivity floor for arbitrary text."""
    sensitive_categories, private_categories = detected_sensitive_categories(
        text,
        credential_requires_value=credential_requires_value,
        contact_requires_value=contact_requires_value,
    )
    if sensitive_categories:
        return "sensitive"
    if private_categories:
        return "private"
    return "normal"
