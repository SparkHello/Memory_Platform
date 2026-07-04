import json
import re

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_memory_batch_extraction_messages,
    render_memory_extraction_messages,
)
from app.memory.extraction_hints import apply_extraction_hints
from app.memory.models import CandidateMemory
from app.memory.review_policy import normalize_time_uncertain_candidate
from app.memory.utils import _parse_json_object
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

        candidate = normalize_time_uncertain_candidate(candidate, source_text=user_message)
        candidate = apply_extraction_hints(candidate, source_text=user_message)
        candidate_json = json.dumps(candidate.model_dump(), ensure_ascii=False)
        rejection = _gate_reason(candidate, user_message)
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
                outcomes=[ExtractionOutcome(reason=f"调用提取模型失败：{exc}")],
                reason=f"调用提取模型失败：{exc}",
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

            candidate = normalize_time_uncertain_candidate(candidate, source_text=source_text)
            candidate = apply_extraction_hints(candidate, source_text=source_text)
            normalized_candidate_json = json.dumps(candidate.model_dump(), ensure_ascii=False)
            rejection = validate_candidate_for_save(
                candidate,
                user_message=source_text,
                require_quote_in_user_message=True,
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


def validate_candidate_for_save(
    candidate: CandidateMemory,
    *,
    user_message: str | None = None,
    require_quote_in_user_message: bool = False,
) -> str | None:
    """逐条核对保存规则，返回拒绝原因；全部通过时返回 None。

    不同类型使用不同的 importance / confidence 阈值：
    - episodic / semantic: importance≥6, confidence≥0.80
    - procedural / reflective: importance≥5, confidence≥0.80
    - emotional: importance≥5, confidence≥0.80
    """
    if candidate.action == "ignore":
        return candidate.reason or "提取模型判定无需保存"
    if not candidate.memory.strip():
        return "memory 内容为空"

    min_imp, min_conf = _TYPE_THRESHOLDS.get(
        candidate.type, (MIN_IMPORTANCE, MIN_CONFIDENCE)
    )
    if candidate.importance < min_imp:
        return f"importance {candidate.importance} 低于保存阈值 {min_imp}（类型: {candidate.type}）"
    if candidate.confidence < min_conf:
        return f"confidence {candidate.confidence} 低于保存阈值 {min_conf}（类型: {candidate.type}）"

    quote = candidate.source_quote.strip()
    quote_rejection = _source_quote_gate_reason(
        quote,
        user_message=user_message,
        require_quote_in_user_message=require_quote_in_user_message,
    )
    explicit_memory_text = user_message if require_quote_in_user_message else quote

    if require_quote_in_user_message:
        sensitive_rejection = _sensitive_gate_reason(candidate, explicit_memory_text or "")
        if sensitive_rejection:
            return sensitive_rejection
        if quote_rejection:
            return quote_rejection
        assumption_text = _sentence_containing(user_message or "", quote)
    else:
        if quote_rejection:
            return quote_rejection
        sensitive_rejection = _sensitive_gate_reason(candidate, explicit_memory_text)
        if sensitive_rejection:
            return sensitive_rejection
        assumption_text = quote

    marker = find_assumption_marker(assumption_text)
    if marker:
        return f"假设场景（命中「{marker}」），不保存"
    return None


def _gate_reason(candidate: CandidateMemory, user_message: str) -> str | None:
    return validate_candidate_for_save(
        candidate,
        user_message=user_message,
        require_quote_in_user_message=True,
    )


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


def _sensitive_gate_reason(candidate: CandidateMemory, explicit_memory_text: str) -> str | None:
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
    if not _has_explicit_memory_marker(explicit_memory_text):
        return "隐私或敏感信息只有在用户明确要求记住时才保存"
    return None


def _has_explicit_memory_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in EXPLICIT_MEMORY_MARKERS)


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
