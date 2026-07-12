import json
import re

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_memory_batch_extraction_messages,
    render_memory_extraction_messages,
)
from app.memory.extraction_hints import apply_extraction_hints
from app.memory.models import CandidateMemory, MemorySensitivity
from app.memory.review_policy import normalize_time_uncertain_candidate
from app.memory.utils import _parse_json_object, _terms
from app.openai_compat.schemas import ChatCompletionRequest

# 命中这些表达的句子被视为假设场景，一律不写入记忆
ASSUMPTION_MARKERS = (
    "如果",
    "假如",
    "假设",
    "比如我用",
    "suppose",
    "if i use",
    "imagine",
    "let's say",
)

MIN_IMPORTANCE = 6  # 默认回退值
MIN_CONFIDENCE = 0.8  # 默认回退值

_TYPE_THRESHOLDS: dict[str, tuple[int, float]] = {
    "episodic": (6, 0.80),
    "semantic": (6, 0.80),
    "procedural": (5, 0.80),
    "emotional": (5, 0.80),
    "reflective": (5, 0.80),
}

SENSITIVE_MIN_IMPORTANCE = 8
SENSITIVE_MIN_CONFIDENCE = 0.9

EXPLICIT_MEMORY_MARKERS = (
    "记住",
    "记得",
    "别忘",
    "不要忘",
    "以后记得",
    "remember",
    "don't forget",
)

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

_CREDENTIAL_VALUE_PATTERN = re.compile(
    r"(?:密码|口令|验证码|密钥|私钥|助记词|password|passcode|"
    r"api[-_ ]?key|access[-_ ]?token|secret[-_ ]?key|private[-_ ]?key)"
    r"\s*(?:是|为|is|=|:|：)?\s*([^\s,，。;；!?！？]{4,})",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[\s()\-./]?){4,}(?!\d)")
_TOKEN_SECRET_PATTERN = re.compile(r"\b(?:sk|pk|token)[-_][A-Za-z0-9_-]{4,}\b", re.IGNORECASE)

_GENERIC_ENTITIES = {"用户", "本人", "我", "user", "me", "myself"}
_GROUNDING_GENERIC_TERMS = {
    "用户",
    "本人",
    "自己",
    "目前",
    "现在",
    "长期",
    "一直",
    "已经",
    "正在",
    "主要",
    "喜欢",
    "偏好",
    "认为",
    "觉得",
    "发现",
    "希望",
    "需要",
    "使用",
    "工作",
    "居住",
    "住在",
    "user",
    "users",
    "likes",
    "like",
    "prefers",
    "prefer",
    "uses",
    "use",
    "currently",
    "now",
}
_GROUNDING_GENERIC_CJK_CHARS = set(
    "用户我本人自己目前现在长期一直已经正在主要比较非常很更最会将把被让从于"
    "对与和跟及以及的了是为有在用喜爱偏好欢觉认发希需想住居工做使"
)
_GROUNDING_NEGATION_MARKERS = (
    "不",
    "无",
    "没",
    "讨厌",
    "停止",
    "戒掉",
    "not",
    "no longer",
    "without",
    "dislike",
    "hate",
)


def detect_text_sensitivity(text: str) -> MemorySensitivity:
    """Return the deterministic local sensitivity floor for arbitrary text."""
    sensitive_categories, private_categories = _detected_sensitive_categories(text)
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


def has_text_grounding_anchor(candidate_text: str, evidence_text: str) -> bool:
    """Return whether candidate text shares a substantive scoped anchor with evidence."""
    return bool(_grounding_evidence_clauses(candidate_text, evidence_text))


class ExtractionOutcome(BaseModel):
    """一次记忆提取的结果：候选记忆 + 是否通过保存校验。"""

    candidate: CandidateMemory | None = None
    accepted: bool = False
    reason: str
    candidate_json: str = ""


class ExtractionBatchOutcome(BaseModel):
    """一段文本拆分出的多条候选记忆结果。"""

    outcomes: list[ExtractionOutcome] = Field(default_factory=list)
    reason: str = ""
    raw_output: str = ""
    retryable_error: bool = False
    error_code: str | None = None


class LLMMemoryExtractor:
    """调用上游模型分析本轮对话，产出符合严格 JSON 格式的候选记忆。"""

    def __init__(self, *, llm_client: OpenAICompatibleClient):
        self.llm_client = llm_client

    async def extract(self, *, user_message: str, assistant_message: str) -> ExtractionOutcome:
        try:
            raw_output = await self._call_llm(
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as exc:
            return ExtractionOutcome(reason=f"调用提取模型失败：{exc}")

        data = _parse_json_object(raw_output)
        if data is None:
            return ExtractionOutcome(
                reason="提取模型输出的不是合法 JSON",
                candidate_json=raw_output[:500],
            )

        try:
            candidate = CandidateMemory.model_validate(data)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            return ExtractionOutcome(
                reason=f"提取输出不符合 schema（字段 {field}）",
                candidate_json=json.dumps(data, ensure_ascii=False)[:500],
            )

        source_rejection = _raw_candidate_source_gate_reason(
            candidate,
            user_message=user_message,
            require_quote_in_user_message=True,
        )
        if source_rejection:
            return ExtractionOutcome(
                candidate=candidate,
                reason=source_rejection,
                candidate_json=json.dumps(candidate.model_dump(), ensure_ascii=False),
            )

        candidate = normalize_time_uncertain_candidate(candidate)
        candidate = apply_extraction_hints(candidate)
        candidate_json = json.dumps(candidate.model_dump(), ensure_ascii=False)
        rejection = _gate_reason(
            candidate,
            user_message,
            source_grounding_checked=True,
        )
        if rejection:
            return ExtractionOutcome(
                candidate=candidate,
                reason=rejection,
                candidate_json=candidate_json,
            )
        return ExtractionOutcome(
            candidate=candidate,
            accepted=True,
            reason=candidate.reason or "通过保存校验",
            candidate_json=candidate_json,
        )

    async def extract_many(
        self,
        *,
        source_text: str,
        assistant_message: str | None = None,
    ) -> ExtractionBatchOutcome:
        try:
            raw_output = await self._call_llm_many(
                source_text=source_text,
                assistant_message=assistant_message,
            )
        except Exception as exc:
            return ExtractionBatchOutcome(
                outcomes=[],
                reason=f"调用提取模型失败：{exc}",
                retryable_error=_is_retryable_upstream_error(exc),
                error_code="upstream_unavailable",
            )

        data = _parse_json_object(raw_output)
        if data is None:
            reason = "提取模型输出的不是合法 JSON"
            return ExtractionBatchOutcome(
                outcomes=[
                    ExtractionOutcome(
                        reason=reason,
                        candidate_json=raw_output[:500],
                    )
                ],
                reason=reason,
                raw_output=raw_output[:500],
            )

        candidate_data = _candidate_payloads_from_data(data)
        if not candidate_data:
            return ExtractionBatchOutcome(
                outcomes=[],
                reason=str(data.get("reason") or "没有值得保存的长期记忆"),
                raw_output=raw_output[:500],
            )

        outcomes: list[ExtractionOutcome] = []
        for item in candidate_data:
            candidate_json = json.dumps(item, ensure_ascii=False)[:500]
            try:
                candidate = CandidateMemory.model_validate(item)
            except ValidationError as exc:
                first_error = exc.errors()[0]
                field = ".".join(str(part) for part in first_error.get("loc", ()))
                outcomes.append(
                    ExtractionOutcome(
                        reason=f"提取输出不符合 schema（字段 {field}）",
                        candidate_json=candidate_json,
                    )
                )
                continue

            source_rejection = _raw_candidate_source_gate_reason(
                candidate,
                user_message=source_text,
                require_quote_in_user_message=True,
            )
            if source_rejection:
                outcomes.append(
                    ExtractionOutcome(
                        candidate=candidate,
                        reason=source_rejection,
                        candidate_json=json.dumps(candidate.model_dump(), ensure_ascii=False),
                    )
                )
                continue

            candidate = normalize_time_uncertain_candidate(candidate)
            candidate = apply_extraction_hints(candidate)
            normalized_candidate_json = json.dumps(candidate.model_dump(), ensure_ascii=False)
            rejection = _validate_candidate_for_save(
                candidate,
                user_message=source_text,
                require_quote_in_user_message=True,
                source_grounding_checked=True,
            )
            if rejection:
                outcomes.append(
                    ExtractionOutcome(
                        candidate=candidate,
                        reason=rejection,
                        candidate_json=normalized_candidate_json,
                    )
                )
                continue
            outcomes.append(
                ExtractionOutcome(
                    candidate=candidate,
                    accepted=True,
                    reason=candidate.reason or "通过保存校验",
                    candidate_json=normalized_candidate_json,
                )
            )

        return ExtractionBatchOutcome(
            outcomes=outcomes,
            reason=str(data.get("reason") or "拆分完成"),
            raw_output=raw_output[:500],
        )

    async def _call_llm(self, *, user_message: str, assistant_message: str) -> str:
        messages = render_memory_extraction_messages(
            user_message=user_message,
            assistant_message=assistant_message,
        )
        request = ChatCompletionRequest(
            model="memory-extractor",
            messages=messages,
            temperature=0.0,
            stream=False,
        )
        response = await self.llm_client.create_chat_completion(request=request, messages=messages)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""

    async def _call_llm_many(
        self,
        *,
        source_text: str,
        assistant_message: str | None,
    ) -> str:
        messages = render_memory_batch_extraction_messages(
            source_text=source_text,
            assistant_message=assistant_message,
        )
        request = ChatCompletionRequest(
            model="memory-ingester",
            messages=messages,
            temperature=0.0,
            stream=False,
        )
        response = await self.llm_client.create_chat_completion(request=request, messages=messages)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""


def _candidate_payloads_from_data(data: dict) -> list[dict]:
    memories = data.get("memories")
    if isinstance(memories, list):
        return [item for item in memories if isinstance(item, dict)]
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    if data.get("action") in {"create", "update", "ignore"}:
        return [data]
    return []


def _is_retryable_upstream_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return True
    return int(status_code) in {408, 429, 502, 503, 504}


def validate_candidate_for_save(
    candidate: CandidateMemory,
    *,
    user_message: str | None = None,
    require_quote_in_user_message: bool = False,
) -> str | None:
    """Validate a candidate against source, grounding, and save thresholds."""
    return _validate_candidate_for_save(
        candidate,
        user_message=user_message,
        require_quote_in_user_message=require_quote_in_user_message,
        source_grounding_checked=False,
    )


def _validate_candidate_for_save(
    candidate: CandidateMemory,
    *,
    user_message: str | None,
    require_quote_in_user_message: bool,
    source_grounding_checked: bool = False,
) -> str | None:
    """逐条核对保存规则，返回拒绝原因；全部通过时返回 None。

    不同类型使用不同的 importance / confidence 阈值：
    - episodic / semantic: importance≥6, confidence≥0.80
    - procedural / reflective: importance≥5, confidence≥0.80
    - emotional: importance≥5, confidence≥0.80
    """
    if candidate.action == "ignore":
        return candidate.reason or "提取模型判定无需保存"

    quote = candidate.source_quote.strip()
    quote_rejection = _source_quote_gate_reason(
        quote,
        user_message=user_message,
        require_quote_in_user_message=require_quote_in_user_message,
    )
    if quote_rejection:
        return quote_rejection

    # Structured direct-save callers are authoritative inputs; grounding and
    # automatic sensitivity upgrades protect model-extracted candidates.
    if require_quote_in_user_message:
        _apply_sensitivity_floor(candidate)
    if require_quote_in_user_message and not source_grounding_checked:
        grounding_rejection = _grounding_gate_reason(candidate, quote=quote)
        if grounding_rejection:
            return grounding_rejection

    if not candidate.memory.strip():
        return "memory 内容为空"

    min_imp, min_conf = _TYPE_THRESHOLDS.get(
        candidate.type, (MIN_IMPORTANCE, MIN_CONFIDENCE)
    )
    if candidate.importance < min_imp:
        return f"importance {candidate.importance} 低于保存阈值 {min_imp}（类型: {candidate.type}）"
    if candidate.confidence < min_conf:
        return f"confidence {candidate.confidence} 低于保存阈值 {min_conf}（类型: {candidate.type}）"

    sensitive_rejection = _sensitive_gate_reason(
        candidate,
        user_message=user_message,
        quote=quote,
    )
    if sensitive_rejection:
        return sensitive_rejection

    assumption_text = (
        _sentence_containing(user_message or "", quote)
        if require_quote_in_user_message
        else quote
    )

    marker = find_assumption_marker(assumption_text)
    if marker:
        return f"假设场景（命中「{marker}」），不保存"
    return None


def _gate_reason(
    candidate: CandidateMemory,
    user_message: str,
    *,
    source_grounding_checked: bool = False,
) -> str | None:
    return _validate_candidate_for_save(
        candidate,
        user_message=user_message,
        require_quote_in_user_message=True,
        source_grounding_checked=source_grounding_checked,
    )


def _raw_candidate_source_gate_reason(
    candidate: CandidateMemory,
    *,
    user_message: str | None,
    require_quote_in_user_message: bool,
) -> str | None:
    """Validate model evidence before any post-extraction mutation."""
    if candidate.action == "ignore":
        return None
    quote = candidate.source_quote.strip()
    quote_rejection = _source_quote_gate_reason(
        quote,
        user_message=user_message,
        require_quote_in_user_message=require_quote_in_user_message,
    )
    if quote_rejection:
        return quote_rejection
    _apply_sensitivity_floor(candidate)
    return _grounding_gate_reason(candidate, quote=quote)


def _source_quote_gate_reason(
    quote: str,
    *,
    user_message: str | None,
    require_quote_in_user_message: bool,
) -> str | None:
    if not quote:
        if require_quote_in_user_message:
            return "缺少 source_quote"
        return "缺少 source_quote（必须提供用户原话的逐字片段）"
    if require_quote_in_user_message and quote not in (user_message or ""):
        return "source_quote 不是用户原话，疑似模型自行编造"
    return None


def _apply_sensitivity_floor(candidate: CandidateMemory) -> None:
    floor = sensitivity_floor(
        candidate.sensitivity,
        candidate.source_quote,
        candidate.memory,
        *candidate.entities,
    )
    if floor == candidate.sensitivity:
        return
    candidate.sensitivity = floor  # type: ignore[assignment]
    suffix = f"代码层敏感检测将 sensitivity 下限提升为 {floor}。"
    candidate.reason = f"{candidate.reason}；{suffix}" if candidate.reason else suffix


def _grounding_gate_reason(candidate: CandidateMemory, *, quote: str) -> str | None:
    memory_sensitive, memory_private = _detected_sensitive_categories(candidate.memory)
    quote_sensitive, quote_private = _detected_sensitive_categories(quote)
    unsupported_categories = (memory_sensitive - quote_sensitive) | (memory_private - quote_private)
    if unsupported_categories:
        categories = ", ".join(sorted(unsupported_categories))
        return f"candidate.memory 的敏感事实缺少 source_quote 支撑（类别: {categories}）"

    for kind, value in sorted(_structured_values(candidate.memory)):
        if not _structured_value_present(value, quote):
            return f"candidate.memory 中的结构化{kind}未出现在 source_quote，疑似模型编造"

    lowered_quote = quote.casefold()
    has_grounded_entity = False
    for entity in candidate.entities:
        normalized_entity = entity.strip()
        if not normalized_entity or normalized_entity.casefold() in _GENERIC_ENTITIES:
            continue
        if normalized_entity.casefold() not in lowered_quote:
            return "candidate.entities 中有值未出现在 source_quote，疑似模型编造"
        has_grounded_entity = True

    evidence_clauses = _grounding_evidence_clauses(candidate.memory, quote)
    if not evidence_clauses and has_grounded_entity:
        evidence_clauses = [quote]
    if not evidence_clauses:
        return "candidate.memory 与 source_quote 缺少共同事实锚点，疑似无关改写"
    if not any(
        _grounding_has_negation(candidate.memory) == _grounding_has_negation(clause)
        for clause in evidence_clauses
    ):
        return "candidate.memory 与 source_quote 的否定含义不一致"
    return None


def _grounding_evidence_clauses(memory: str, quote: str) -> list[str]:
    memory_terms = _grounding_terms(memory)
    if not memory_terms:
        return []
    clauses = [
        clause.strip()
        for clause in re.split(
            r"[。！？!?;；\n,，、]+|(?:但是|不过|然而|同时|并且|但)|\b(?:but|however|while)\b",
            quote,
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    scored = [
        (len(memory_terms & _grounding_terms(clause)), clause)
        for clause in clauses or [quote]
    ]
    best_score = max((score for score, _ in scored), default=0)
    if best_score <= 0:
        return []
    return [clause for score, clause in scored if score == best_score]


def _grounding_terms(text: str) -> set[str]:
    grounded: set[str] = set()
    for term in _terms(text):
        lowered = term.casefold()
        if lowered in _GROUNDING_GENERIC_TERMS:
            continue
        cjk_chars = [char for char in lowered if "\u4e00" <= char <= "\u9fff"]
        if cjk_chars and all(char in _GROUNDING_GENERIC_CJK_CHARS for char in cjk_chars):
            continue
        if not cjk_chars and len(lowered) < 2:
            continue
        grounded.add(lowered)
    return grounded


def _grounding_has_negation(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _GROUNDING_NEGATION_MARKERS)


def _detected_sensitive_categories(text: str) -> tuple[set[str], set[str]]:
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


def _structured_values(text: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    values.update(("邮箱", match.group(0)) for match in _EMAIL_PATTERN.finditer(text))
    values.update(("数字", match.group(0)) for match in _LONG_NUMBER_PATTERN.finditer(text))
    values.update(("密钥", match.group(0)) for match in _TOKEN_SECRET_PATTERN.finditer(text))
    values.update(("凭据", match.group(1)) for match in _CREDENTIAL_VALUE_PATTERN.finditer(text))
    return values


def _structured_value_present(value: str, quote: str) -> bool:
    compact_value = re.sub(r"[\s()\-./]", "", value).casefold().strip("'\"`。.!！?")
    compact_quote = re.sub(r"[\s()\-./]", "", quote).casefold()
    return bool(compact_value) and compact_value in compact_quote


def _sensitive_gate_reason(
    candidate: CandidateMemory,
    *,
    user_message: str | None,
    quote: str,
) -> str | None:
    if candidate.sensitivity == "normal":
        return None
    if candidate.importance < SENSITIVE_MIN_IMPORTANCE:
        return (
            f"{candidate.sensitivity} 记忆 importance {candidate.importance} "
            f"低于敏感信息保存阈值 {SENSITIVE_MIN_IMPORTANCE}"
        )
    if candidate.confidence < SENSITIVE_MIN_CONFIDENCE:
        return (
            f"{candidate.sensitivity} 记忆 confidence {candidate.confidence} "
            f"低于敏感信息保存阈值 {SENSITIVE_MIN_CONFIDENCE}"
        )
    evidence_quotes = _sensitive_evidence_quotes(candidate, quote)
    if not all(
        _has_scoped_explicit_memory_marker(user_message or quote, evidence_quote)
        for evidence_quote in evidence_quotes
    ):
        return "隐私或敏感信息只有在用户明确要求记住时才保存"
    return None


def _sensitive_evidence_quotes(candidate: CandidateMemory, quote: str) -> list[str]:
    clauses = [
        clause.strip()
        for clause in re.split(r"[。！？!?;；\n,，、:：]", quote)
        if clause.strip()
    ]
    structured_values = _structured_values(candidate.memory)
    value_clauses = [
        clause
        for clause in clauses
        if any(_structured_value_present(value, clause) for _, value in structured_values)
    ]
    if value_clauses:
        return value_clauses

    memory_sensitive, memory_private = _detected_sensitive_categories(candidate.memory)
    category_clauses: list[str] = []
    for clause in clauses:
        clause_sensitive, clause_private = _detected_sensitive_categories(clause)
        if (memory_sensitive & clause_sensitive) or (memory_private & clause_private):
            category_clauses.append(clause)
    return category_clauses or [quote]


def _has_explicit_memory_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in EXPLICIT_MEMORY_MARKERS)


def _has_scoped_explicit_memory_marker(message: str, quote: str) -> bool:
    """Require sensitive-memory authorization next to the quoted fact."""
    if _has_explicit_memory_marker(quote):
        return True
    for clause, previous_clause, next_clause in _clauses_around_quote(message, quote):
        if _has_explicit_memory_marker(clause):
            return True
        if _is_standalone_memory_directive(previous_clause):
            return True
        if _is_standalone_memory_directive(next_clause):
            return True
    return False


def _clauses_around_quote(message: str, quote: str) -> list[tuple[str, str, str]]:
    contexts: list[tuple[str, str, str]] = []
    start = 0
    while quote and (index := message.find(quote, start)) >= 0:
        sentence_start = max(
            (message.rfind(marker, 0, index) for marker in "。！？!?;；\n"),
            default=-1,
        ) + 1
        sentence_end_candidates = [
            end
            for marker in "。！？!?;；\n"
            if (end := message.find(marker, index + len(quote))) >= 0
        ]
        sentence_end = min(sentence_end_candidates, default=len(message))
        sentence = message[sentence_start:sentence_end]
        relative_index = index - sentence_start
        parts = re.split(r"[,，、:：]", sentence)
        cursor = 0
        for part_index, part in enumerate(parts):
            part_end = cursor + len(part)
            if cursor <= relative_index <= part_end:
                previous_part = parts[part_index - 1] if part_index > 0 else ""
                next_part = parts[part_index + 1] if part_index + 1 < len(parts) else ""
                contexts.append((part, previous_part, next_part))
                break
            cursor = part_end + 1
        start = index + max(1, len(quote))
    return contexts


def _is_standalone_memory_directive(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    chinese = re.fullmatch(
        r"(?:请|请你|麻烦你?|务必|一定要|帮我)?\s*"
        r"(?:记住|记得|别忘|不要忘|以后记得)"
        r"(?:这(?:条|件)?(?:信息|事)?)?",
        stripped,
    )
    english = re.fullmatch(
        r"(?:please\s+)?(?:remember|don't forget)(?:\s+this)?",
        stripped,
    )
    return chinese is not None or english is not None


def _sentence_containing(message: str, quote: str) -> str:
    """取 source_quote 所在的句子；引用跨句时保守地返回整条消息。"""
    for sentence in re.split(r"[。！？!?;；\n]", message):
        if quote in sentence:
            return sentence
    return message


def find_assumption_marker(text: str) -> str | None:
    """返回文本命中的第一个假设表达，未命中返回 None。MCP 层也用它做保存门槛。"""
    lowered = text.lower()
    for marker in ASSUMPTION_MARKERS:
        if marker in lowered:
            return marker
    return None
