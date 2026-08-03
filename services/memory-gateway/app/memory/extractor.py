import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_memory_batch_extraction_messages,
    render_memory_extraction_messages,
)
from app.memory.extraction_hints import apply_extraction_hints
from app.memory.models import CandidateMemory
from app.memory.redaction import (
    detected_sensitive_categories,
    sensitivity_floor,
)
from app.memory.review_policy import normalize_time_uncertain_candidate
from app.memory.utils import _has_negation, _parse_json_object, _terms
from app.openai_compat.schemas import ChatCompletionRequest
from app.usage.context import model_usage_scope

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

ExtractionReasonCode = Literal[
    "has_candidates",
    "no_long_term_value",
    "temporary_or_one_off",
    "hypothetical_or_uncertain",
    "not_user_asserted",
    "sensitive_without_explicit_request",
    "insufficient_context",
    "other",
    "unclassified",
    "invalid_model_output",
    "upstream_unavailable",
]

_MODEL_EXTRACTION_REASON_CODES = frozenset(
    {
        "has_candidates",
        "no_long_term_value",
        "temporary_or_one_off",
        "hypothetical_or_uncertain",
        "not_user_asserted",
        "sensitive_without_explicit_request",
        "insufficient_context",
        "other",
    }
)

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
_CURRENT_AGE_QUOTE_PATTERN = re.compile(
    r"(?:我|本人)?\s*(?:现在|目前|今年)\s*(\d{1,3})\s*岁"
    r"|(?:i am|i'm)\s*(?:currently\s*)?(\d{1,3})(?:\s*years?\s+old)?",
    re.IGNORECASE,
)
_AGE_MEMORY_PATTERN = re.compile(
    r"(?:自称|现在|目前|今年)\s*(\d{1,3})\s*岁"
    r"|年龄\s*(?:是|为)?\s*(\d{1,3})\s*岁?"
    r"|(\d{1,3})\s*岁"
    r"|(?:currently\s*)?(\d{1,3})(?:\s*years?\s+old)",
    re.IGNORECASE,
)
_BARE_AGE_ANSWER_PATTERN = re.compile(r"^\s*(\d{1,3})\s*[。.!！]?\s*$")
_AGE_CONTEXT_PATTERN = re.compile(
    r"(?:多少\s*岁|多大(?:了)?|年龄(?:是)?多少|几岁)"
    r"|\bhow\s+old\b|\bage\b",
    re.IGNORECASE,
)
_CURRENT_DATE_PREFIX_PATTERN = re.compile(
    r"^\s*截至\s*"
    r"(?P<year>\d{4})\s*(?:-|/|\.|年)\s*"
    r"(?P<month>\d{1,2})"
    r"(?:\s*(?:-|/|\.|月)\s*(?P<day>\d{1,2})\s*日?)?"
    r"\s*[,，:：]?\s*"
)

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
    "i",
    "me",
    "my",
    "mine",
    "we",
    "our",
    "the",
    "a",
    "an",
    "and",
    "or",
    "in",
    "at",
    "for",
    "of",
    "to",
    "from",
    "on",
    "with",
}
_GROUNDING_GENERIC_CJK_CHARS = set(
    "用户我本人自己目前现在长期一直已经正在主要比较非常很更最会将把被让从于"
    "对与和跟及以及的了是为有在用喜爱偏好欢觉认发希需想住居工做使"
)
_GROUNDING_MEMORY_SPLIT_PATTERN = re.compile(
    r"[。！？!?;；\n,，、]+"
    r"|(?:但是|不过|然而|同时|而且|此外|并且)"
    r"|并(?=\s*(?:在|住|居|任|就|喜|爱|讨|使|用|有|是|叫|认|觉|发|希|需|想|工作))"
    r"|和(?=\s*(?:住在|居住|工作|任职|就职|喜欢|讨厌|使用|拥有|是|叫))"
    r"|(?:还|也|又|兼)(?=\s*(?:在|住|居|任|就|入职|喜|爱|讨|使|用|有|是|叫|买|购|卖|销售|申请|旅行|旅游|工作))"
    r"|\b(?:but|however|while|also|additionally|moreover)\b"
    r"|\band\b(?=\s*(?:(?:the\s+)?user|they|he|she|i)?\s*"
    r"(?:lives?|resides?|works?|uses?|likes?|prefers?|hates?|has|have|is|are|"
    r"wants?|plans?|appl(?:y|ies|ied)|buys?|bought|purchases?|sells?|sold|"
    r"visits?|travels?|moves?|owns?|drinks?|eats?|needs?|intends?|hopes?))",
    flags=re.IGNORECASE,
)
_GROUNDING_EVIDENCE_SPLIT_PATTERN = re.compile(
    r"[。！？!?;；\n,，、]+"
    r"|(?:但是|不过|然而|同时|并且|但)"
    r"|\b(?:but|however|while)\b",
    flags=re.IGNORECASE,
)

_CHINESE_MEMORY_DIRECTIVE_PATTERN = re.compile(
    r"^\s*(?:(?:请|请你|麻烦你?|务必|一定要|帮我|你要|你得|我希望你|我要你)\s*)?"
    r"(?:以后\s*)?(?:记住|记得|别忘(?:了)?|不要忘(?:了)?)"
    r"(?=$|\s|[,，:：]|我|这|以下)",
)
_ENGLISH_MEMORY_DIRECTIVE_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)|(?:(?:can|could|would|will)\s+you\s+)|"
    r"(?:i\s+(?:want|need)\s+you\s+to\s+))?"
    r"(?:remember(?!\s+(?:when|whether|how|that\s+time)\b)|"
    r"do\s+not\s+forget|don't\s+forget)\b",
    flags=re.IGNORECASE,
)

# Grounding must bind both the object and the asserted relation.  A shared
# entity alone (``Acme``, ``Beijing`` or ``coffee``) cannot distinguish
# employment from an application, residence from a visit, or preference from
# a purchase.  These deliberately broad bilingual families canonicalize the
# common long-term-memory predicates; unknown predicates fall back to a much
# stricter lexical-coverage rule below.
_GROUNDING_RELATION_PATTERNS: dict[str, tuple[str, ...]] = {
    "preference": (
        r"喜欢|喜爱|偏好|钟爱|最爱|讨厌|厌恶|反感|不喜欢",
        r"\b(?:like|likes|liked|prefer|prefers|preferred|enjoy|enjoys|favorite|"
        r"favourite|dislike|dislikes|disliked|hate|hates|hated)\b",
    ),
    "residence": (
        r"住在|居住|常住|定居|住所|居所|搬到|搬去|"
        r"(?:我|本人|用户)(?:现在|目前|当前)?住(?!房|宅|宿|院|店)",
        r"\b(?:live|lives|lived|living|reside|resides|resided|residing)\b|"
        r"\bbased\s+in\b|\bmoved?\s+to\b",
    ),
    "employment": (
        r"工作|任职|就职|入职|受雇|雇主|职业",
        r"\b(?:work|works|worked|working|employ|employs|employed|employer|"
        r"occupation|job)\b",
    ),
    "application": (
        r"申请|应聘|求职|投递",
        r"\b(?:apply|applies|applied|applying|application|applications|candidate)\b",
    ),
    "visit": (
        r"旅游|旅行|游览|去过|访问|出差",
        r"\b(?:visit|visits|visited|visiting|travel|travels|traveled|travelled|"
        r"traveling|travelling|tour|toured|trip)\b",
    ),
    "purchase": (
        r"购买|购入|采购|买了|想买|打算买",
        r"\b(?:buy|buys|buying|bought|purchase|purchases|purchased|purchasing)\b",
    ),
    "sale": (
        r"销售|售出|出售|卖了|卖掉",
        r"\b(?:sell|sells|selling|sold|sale|sales)\b",
    ),
    "usage": (
        r"使用|采用|常用|主力|默认|切换到|改用|"
        r"换成|换为|改成|改为|(?<!使)用(?!户)",
        r"\b(?:use|uses|used|using|utilize|utilizes|utilized|rely|relies|relied)\b|"
        r"\bswitched?\s+to\b",
    ),
    "possession": (
        r"拥有|我有|用户有|本人有|没有|"
        r"(?:用户|我|本人)无(?:任何)?|养了|养着",
        r"\b(?:have|has|had|own|owns|owned|owning)\b",
    ),
    "identity": (
        r"名字|姓名|叫我|称呼我|身份证|护照|证件",
        r"\b(?:name|named|call\s+me|identity|passport|identification)\b",
    ),
    "tool_choice": (
        r"AI\s*客户端|客户端|主力设备|主力手机|主力电脑|"
        r"设备|手机|电脑|换成|换为|改成|改为",
        r"\b(?:ai\s+client|client|primary\s+device|phone|computer|laptop|device)\b|"
        r"\bswitched?\s+to\b",
    ),
    "age": (
        r"年龄|多少\s*岁|几\s*岁|多大(?:了)?|\d{1,3}\s*岁",
        r"\bage\b|\bhow\s+old\b|\b\d{1,3}\s+years?\s+old\b",
    ),
    "belief": (
        r"认为|觉得|相信|发现|意识到",
        r"\b(?:think|thinks|thought|believe|believes|believed|realize|realized|"
        r"realise|realised)\b",
    ),
    "intent": (
        r"希望|计划|打算|想要|准备|目标",
        r"\b(?:hope|hopes|hoped|plan|plans|planned|intend|intends|intended|"
        r"want|wants|wanted|goal|goals)\b",
    ),
    "need": (
        r"需要|必须|要求|依赖",
        r"\b(?:need|needs|needed|require|requires|required|must|depend|depends)\b",
    ),
    "consumption": (
        r"喝|饮用|吃|食用",
        r"\b(?:drink|drinks|drank|drunk|eat|eats|ate|consume|consumes|consumed)\b",
    ),
    "creation": (
        r"制造|生产|创作|制作|开发",
        r"\b(?:make|makes|made|manufacture|manufactures|manufactured|produce|"
        r"produces|produced|create|creates|created|develop|develops|developed)\b",
    ),
    "relationship": (
        r"妻子|丈夫|伴侣|男朋友|女朋友|家人|父母|孩子|同事",
        r"\b(?:wife|husband|partner|boyfriend|girlfriend|parent|parents|child|"
        r"children|colleague|coworker)\b",
    ),
}
_GENERIC_MINIMIZED_SENSITIVE_MEMORY = re.compile(
    r"证件信息|身份信息|隐私信息|敏感信息|"
    r"\b(?:identity|identification|credential|private|sensitive)\s+information\b",
    flags=re.IGNORECASE,
)

_THIRD_PARTY_RELATION_SUBJECTS = (
    "猫|狗|宠物|朋友|同事|妻子|丈夫|伴侣|父亲|母亲|父母|孩子|"
    "儿子|女儿|老师|客户|室友|老板"
)
_CANDIDATE_THIRD_PARTY_SUBJECT_RE = re.compile(
    rf"^\s*(?:用户|本人)(?:的|家(?:的)?)?\s*(?:{_THIRD_PARTY_RELATION_SUBJECTS})"
    r"|^\s*(?:the\s+)?user['’]s\s+(?:cat|dog|pet|friend|colleague|coworker|"
    r"wife|husband|partner|parent|child|teacher|client|roommate|boss)\b",
    flags=re.IGNORECASE,
)
_DIRECT_USER_SUBJECT_RE = re.compile(
    r"^\s*(?:用户|本人)(?!\s*(?:的|家(?:的)?)?\s*(?:"
    + _THIRD_PARTY_RELATION_SUBJECTS
    + r"))"
    r"|^\s*(?:the\s+)?user(?!['’]s\s+(?:cat|dog|pet|friend|colleague|"
    r"coworker|wife|husband|partner|parent|child|teacher|client|roommate|boss)\b)"
    r"|^\s*i\b",
    flags=re.IGNORECASE,
)
_THIRD_PARTY_EVIDENCE_SUBJECT_RE = re.compile(
    rf"^\s*(?:关于\s*)?(?:(?:我|用户|本人)(?:的|家(?:的)?)?\s*)?"
    rf"(?:{_THIRD_PARTY_RELATION_SUBJECTS})(?:们)?(?:的)?"
    r"|^\s*(?:他|她|它|他们|她们|它们)(?:的)?"
    r"|^\s*(?:about\s+)?(?:my|the\s+user['’]s|his|her|their|the)\s+"
    r"(?:cat|dog|pet|friend|colleague|coworker|wife|husband|partner|parent|"
    r"child|teacher|client|roommate|boss)\b"
    r"|^\s*(?:he|she|they|it)\b",
    flags=re.IGNORECASE,
)
_JOINT_FIRST_PERSON_SUBJECT_RE = re.compile(
    r"^\s*(?:我\s*(?:和|与|跟)|(?:我的?\s*)?(?:朋友|同事|伴侣|家人)\s*(?:和|与|跟)\s*我)"
    r"|^\s*(?:i\s+and\b|my\s+.+?\s+and\s+i\b)",
    flags=re.IGNORECASE,
)

# For state-like relations, the relation word and the shared object must belong
# to the same local assertion.  Merely seeing both somewhere in one sentence is
# unsafe: "applied to Acme's job" is not employment, and "visited Beijing and
# stayed at a hotel" is not residence in Beijing.
_BOUND_RELATION_ASSERTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "employment": (
        r"(?:在|为)\s*[^,，。！？!?;；]{1,60}?(?:工作|任职|就职)",
        r"(?:任职|就职)(?:于|在)\s*[^,，。！？!?;；]{1,60}",
        r"(?:雇主|所在公司|就职公司|工作单位)\s*(?:是|为|在)\s*[^,，。！？!?;；]{1,60}",
        r"[^,，。！？!?;；]{1,60}?\s*(?:是|为)\s*(?:我的|用户的|本人的)?(?:雇主|工作单位)",
        r"\b(?:work(?:s|ed|ing)?\s+(?:at|for)|(?:am|is|are|was|were)\s+employed\s+(?:at|by))\s+[^,.;!?]{1,80}",
        r"\b(?:my|the\s+user['’]s)\s+employer\s+is\s+[^,.;!?]{1,80}",
        r"\b[^,.;!?]{1,60}?\s+employ(?:s|ed)\s+me\b",
    ),
    "residence": (
        r"(?:住在|居住(?:在)?|常住(?:在)?|定居(?:在)?|搬到|搬去)\s*[^,，。！？!?;；]{1,60}",
        r"(?:我|本人|用户)\s*(?:现在|目前|当前)?\s*住(?:在)?\s*[^,，。！？!?;；]{1,60}",
        r"(?:住址|住所|居所|居住地)\s*(?:是|为|在)\s*[^,，。！？!?;；]{1,60}",
        r"\b(?:live|lives|lived|living|reside|resides|resided|residing)\s+in\s+[^,.;!?]{1,80}",
        r"\b(?:am|is|are|was|were)\s+based\s+in\s+[^,.;!?]{1,80}",
        r"\bmoved?\s+to\s+[^,.;!?]{1,80}",
    ),
}


def has_text_grounding_anchor(candidate_text: str, evidence_text: str) -> bool:
    """Return whether every candidate proposition is relation- and polarity-grounded."""
    matches = _grounding_proposition_matches(candidate_text, evidence_text)
    return bool(matches) and all(
        any(
            _grounding_has_negation(proposition)
            == _grounding_has_negation(evidence_clause)
            for evidence_clause in evidence_clauses
        )
        for proposition, evidence_clauses in matches
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
    reason_code: ExtractionReasonCode = "unclassified"
    raw_output: str = ""
    retryable_error: bool = False
    error_code: str | None = None


class LLMMemoryExtractor:
    """调用上游模型分析本轮对话，产出符合严格 JSON 格式的候选记忆。"""

    def __init__(
        self,
        *,
        llm_client: OpenAICompatibleClient,
        user_id: str = "default",
    ):
        self.llm_client = llm_client
        self.user_id = user_id

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

        candidate = _clear_unsupported_temporal_dates(candidate)
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
        conversation_context: str | None = None,
        context_quote_source: str | None = None,
    ) -> ExtractionBatchOutcome:
        try:
            raw_output = await self._call_llm_many(
                source_text=source_text,
                assistant_message=assistant_message,
                conversation_context=conversation_context,
            )
        except Exception as exc:
            return ExtractionBatchOutcome(
                outcomes=[],
                reason=f"调用提取模型失败：{exc}",
                reason_code="upstream_unavailable",
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
                reason_code="invalid_model_output",
                raw_output=raw_output[:500],
            )

        candidate_data = _candidate_payloads_from_data(data)
        if not candidate_data:
            return ExtractionBatchOutcome(
                outcomes=[],
                reason=str(data.get("reason") or "没有值得保存的长期记忆"),
                reason_code=_batch_reason_code(data, has_candidates=False),
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
                conversation_context=context_quote_source,
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

            candidate = _clear_unsupported_temporal_dates(candidate)
            candidate = normalize_time_uncertain_candidate(
                candidate,
                source_text=context_quote_source,
            )
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
            reason_code=_batch_reason_code(data, has_candidates=True),
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
        with model_usage_scope(user_id=self.user_id):
            response = await self.llm_client.create_chat_completion(
                request=request,
                messages=messages,
            )
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
        conversation_context: str | None,
    ) -> str:
        messages = render_memory_batch_extraction_messages(
            source_text=source_text,
            assistant_message=assistant_message,
            conversation_context=conversation_context,
        )
        request = ChatCompletionRequest(
            model="memory-ingester",
            messages=messages,
            temperature=0.0,
            stream=False,
        )
        with model_usage_scope(user_id=self.user_id):
            response = await self.llm_client.create_chat_completion(
                request=request,
                messages=messages,
            )
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


def _batch_reason_code(
    data: dict,
    *,
    has_candidates: bool,
) -> ExtractionReasonCode:
    if has_candidates:
        return "has_candidates"
    raw = str(data.get("reason_code") or "").strip().lower()
    if raw in _MODEL_EXTRACTION_REASON_CODES and raw != "has_candidates":
        return raw  # type: ignore[return-value]
    return "unclassified"


def _clear_unsupported_temporal_dates(candidate: CandidateMemory) -> CandidateMemory:
    """Do not let model-invented dates mutate the temporal timeline.

    Current-state words such as "now" are not evidence for an arbitrary date.
    Backend-owned normalizers may add their own trusted clock anchors after this
    check (for example, the current-age review window).
    """
    cleared: list[str] = []
    for field_name in ("valid_from", "valid_until", "review_after"):
        value = getattr(candidate, field_name)
        if value and not _iso_date_supported_by_quote(value, candidate.source_quote):
            setattr(candidate, field_name, None)
            cleared.append(field_name)
    if cleared:
        suffix = "缺少 source_quote 中的明确日期证据，已清空 " + ", ".join(cleared) + "。"
        candidate.reason = f"{candidate.reason}；{suffix}" if candidate.reason else suffix
    return candidate


def _iso_date_supported_by_quote(value: str, quote: str) -> bool:
    parsed = datetime.fromisoformat(value)
    normalized_quote = quote.casefold()
    date_text = parsed.date().isoformat()
    if value.casefold() in normalized_quote or date_text in normalized_quote:
        return True

    year = parsed.year
    month = parsed.month
    day = parsed.day
    full_date_patterns = (
        rf"(?<!\d){year}\s*[年/.-]\s*0?{month}\s*[月/.-]\s*0?{day}\s*日?",
        rf"(?<!\d)0?{month}\s*[月/.-]\s*0?{day}\s*日?\s*[,，]?\s*{year}(?!\d)",
    )
    if any(re.search(pattern, quote, re.IGNORECASE) for pattern in full_date_patterns):
        return True
    if day == 1 and re.search(
        rf"(?<!\d){year}\s*[年/.-]\s*0?{month}\s*月?(?!\d)",
        quote,
        re.IGNORECASE,
    ):
        return True
    return month == 1 and day == 1 and re.search(
        rf"(?<!\d){year}\s*年?(?!\d)",
        quote,
        re.IGNORECASE,
    ) is not None


def _is_retryable_upstream_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return True
    try:
        normalized_status = int(status_code)
    except (TypeError, ValueError):
        return False
    return normalized_status in {408, 429} or normalized_status >= 500


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
    conversation_context: str | None = None,
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
    context_rejection = _context_quote_gate_reason(
        candidate,
        conversation_context=conversation_context,
    )
    if context_rejection:
        return context_rejection
    _apply_sensitivity_floor(candidate)
    grounding_candidate = _candidate_for_raw_grounding(
        candidate,
        quote=quote,
        context_quote_verified=bool(candidate.context_quote.strip()),
    )
    return _grounding_gate_reason(
        grounding_candidate,
        quote=quote,
        relation_context=(
            candidate.context_quote.strip() if candidate.context_quote.strip() else ""
        ),
    )


def _candidate_for_raw_grounding(
    candidate: CandidateMemory,
    *,
    quote: str,
    context_quote_verified: bool = False,
    now: datetime | None = None,
) -> CandidateMemory:
    """Remove only a trusted system-date prefix from a current-age candidate.

    The extraction prompt includes the current date. Older prompt versions also
    asked the model to put that date into ``candidate.memory`` even though it
    could not appear in the verbatim user quote. Treat that exact current-date
    prefix as backend metadata only when the quote and memory carry the same
    current age. Every other structured value remains subject to grounding.
    """
    prefix_match = _CURRENT_DATE_PREFIX_PATTERN.match(candidate.memory)
    if prefix_match is None:
        return candidate

    quote_age = _matched_age(_CURRENT_AGE_QUOTE_PATTERN.search(quote))
    if quote_age is None and context_quote_verified:
        quote_age = _contextual_age_answer(quote, candidate.context_quote)
    memory_age = _matched_age(_AGE_MEMORY_PATTERN.search(candidate.memory[prefix_match.end() :]))
    if quote_age is None or quote_age != memory_age:
        return candidate

    base = now or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    else:
        base = base.astimezone(UTC)
    if int(prefix_match.group("year")) != base.year:
        return candidate
    if int(prefix_match.group("month")) != base.month:
        return candidate
    day = prefix_match.group("day")
    if day is not None and int(day) != base.day:
        return candidate

    grounded_memory = candidate.memory[prefix_match.end() :].strip()
    if not grounded_memory:
        return candidate
    return candidate.model_copy(update={"memory": grounded_memory})


def _matched_age(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    raw_age = next((value for value in match.groups() if value is not None), None)
    if raw_age is None:
        return None
    age = int(raw_age)
    return age if 0 < age < 130 else None


def _context_quote_gate_reason(
    candidate: CandidateMemory,
    *,
    conversation_context: str | None,
) -> str | None:
    context_quote = candidate.context_quote.strip()
    if context_quote:
        if not conversation_context:
            return "candidate.context_quote 缺少较早对话上下文支撑"
        if context_quote not in conversation_context:
            return "context_quote 不是较早对话原文，疑似模型自行编造"

    if _bare_age_answer(candidate.source_quote, candidate.memory) is not None:
        if not context_quote:
            return "仅凭数字无法判断年龄语义，缺少 context_quote"
        if not _AGE_CONTEXT_PATTERN.search(context_quote):
            return "context_quote 未明确询问年龄，无法解释本轮数字回答"
    return None


def _contextual_age_answer(source_quote: str, context_quote: str) -> int | None:
    if not _AGE_CONTEXT_PATTERN.search(context_quote):
        return None
    return _bare_age_answer(source_quote, "")


def _bare_age_answer(source_quote: str, memory: str) -> int | None:
    match = _BARE_AGE_ANSWER_PATTERN.fullmatch(source_quote)
    if match is None:
        return None
    age = int(match.group(1))
    if not 0 < age < 130:
        return None
    if memory:
        memory_age = _matched_age(_AGE_MEMORY_PATTERN.search(memory))
        if memory_age != age:
            return None
    return age


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


def _grounding_gate_reason(
    candidate: CandidateMemory,
    *,
    quote: str,
    relation_context: str = "",
) -> str | None:
    memory_sensitive, memory_private = detected_sensitive_categories(candidate.memory)
    quote_sensitive, quote_private = detected_sensitive_categories(quote)
    unsupported_categories = (memory_sensitive - quote_sensitive) | (memory_private - quote_private)
    if unsupported_categories:
        categories = ", ".join(sorted(unsupported_categories))
        return f"candidate.memory 的敏感事实缺少 source_quote 支撑（类别: {categories}）"

    for kind, value in sorted(_structured_values(candidate.memory)):
        if not _structured_value_present(value, quote):
            return f"candidate.memory 中的结构化{kind}未出现在 source_quote，疑似模型编造"

    lowered_quote = quote.casefold()
    for entity in candidate.entities:
        normalized_entity = entity.strip()
        if not normalized_entity or normalized_entity.casefold() in _GENERIC_ENTITIES:
            continue
        if normalized_entity.casefold() not in lowered_quote:
            return "candidate.entities 中有值未出现在 source_quote，疑似模型编造"
        if normalized_entity.casefold() not in candidate.memory.casefold():
            # Detailed sensitive entities are intentionally removed before
            # persistence.  They may ground only a deliberately minimized
            # sensitive statement below; a normal candidate must bind every
            # declared entity to its own proposition.
            if candidate.sensitivity == "normal":
                return "candidate.entities 中有值未绑定到 candidate.memory 命题"
            continue

    proposition_matches = _grounding_proposition_matches(
        candidate.memory,
        quote,
        relation_context=relation_context,
    )
    # Preserve privacy-minimized sensitive facts such as "用户有一项证件信息"
    # without letting a separately listed entity authorize an arbitrary normal
    # proposition.  The raw value must be verbatim, the candidate must be
    # explicitly sensitive, and its text must declare that it is a generic
    # sensitive-information statement.
    if (
        not proposition_matches
        and candidate.sensitivity != "normal"
        and _GENERIC_MINIMIZED_SENSITIVE_MEMORY.search(candidate.memory)
        and any(
            entity.strip()
            and entity.strip().casefold() not in _GENERIC_ENTITIES
            and entity.strip().casefold() in lowered_quote
            for entity in candidate.entities
        )
    ):
        propositions = _grounding_propositions(candidate.memory)
        if len(propositions) == 1:
            proposition_matches = [(propositions[0], [quote])]
    if not proposition_matches:
        return "candidate.memory 的每个事实必须在 source_quote 中有共同事实锚点"
    for proposition, evidence_clauses in proposition_matches:
        if not any(
            _grounding_has_negation(proposition) == _grounding_has_negation(clause)
            for clause in evidence_clauses
        ):
            return "candidate.memory 与 source_quote 的否定含义不一致"
    return None


def _grounding_evidence_clauses(memory: str, quote: str) -> list[str]:
    """Return evidence only when every independent candidate fact is grounded."""
    matches = _grounding_proposition_matches(memory, quote)
    if not matches:
        return []
    evidence: list[str] = []
    for _, clauses in matches:
        for clause in clauses:
            if clause not in evidence:
                evidence.append(clause)
    return evidence


def _grounding_proposition_matches(
    memory: str,
    quote: str,
    *,
    relation_context: str = "",
) -> list[tuple[str, list[str]]]:
    propositions = _grounding_propositions(memory)
    evidence_clauses = [
        clause.strip()
        for clause in _GROUNDING_EVIDENCE_SPLIT_PATTERN.split(quote)
        if clause.strip()
    ] or [quote]
    if not propositions:
        return []

    matches: list[tuple[str, list[str]]] = []
    for proposition in propositions:
        proposition_terms = _grounding_terms(proposition)
        if not proposition_terms:
            return []
        scored = [
            (
                _grounding_pair_score(
                    proposition,
                    clause,
                    relation_context=relation_context,
                ),
                clause,
            )
            for clause in evidence_clauses
        ]
        best_score = max((score for score, _ in scored), default=0)
        if best_score <= 0:
            return []
        matches.append(
            (proposition, [clause for score, clause in scored if score == best_score])
        )
    return matches


def _grounding_propositions(memory: str) -> list[str]:
    return [
        clause.strip()
        for clause in _GROUNDING_MEMORY_SPLIT_PATTERN.split(memory)
        if clause.strip()
    ]


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


def _grounding_pair_score(
    proposition: str,
    evidence_clause: str,
    *,
    relation_context: str = "",
) -> int:
    """Score a proposition/evidence pair only when object and relation agree.

    One shared proper noun is an object anchor, not proof of the asserted
    relationship.  Known relation families must be present in the evidence;
    unknown predicates need substantially stronger lexical coverage.
    """
    if not _grounding_subjects_compatible(proposition, evidence_clause):
        return 0

    proposition_terms = _grounding_terms(proposition)
    evidence_terms = _grounding_terms(evidence_clause)
    shared_count = len(proposition_terms & evidence_terms)
    structured_anchor = any(
        _structured_value_present(value, evidence_clause)
        for _, value in _structured_values(proposition)
    )

    proposition_relations = _grounding_relation_families(proposition)
    evidence_relations = _grounding_relation_families(evidence_clause)
    if relation_context:
        # A verified earlier utterance may disambiguate the predicate of a bare
        # answer (for example age), but object/value anchors above still come
        # exclusively from the current verbatim source quote.
        evidence_relations |= _grounding_relation_families(relation_context)
    if proposition_relations:
        # A multi-fact candidate that escaped the clause splitter still cannot
        # be supported by evidence for only one of its asserted relations.
        if not proposition_relations.issubset(evidence_relations):
            return 0
        if shared_count <= 0 and not structured_anchor:
            return 0
        for relation in proposition_relations & _BOUND_RELATION_ASSERTION_PATTERNS.keys():
            if not _relation_is_bound_to_asserted_object(
                relation,
                evidence_clause=evidence_clause,
                proposition_terms=proposition_terms,
                proposition_structured_values=_structured_values(proposition),
            ):
                return 0
        return shared_count + len(proposition_relations) * 4 + int(structured_anchor) * 2

    # Unknown relations do not get the one-entity escape hatch.  Exact or near-
    # exact paraphrases remain viable, but a candidate sharing only "Acme" or
    # "Beijing" with a known evidence relation is rejected.
    if structured_anchor and shared_count > 0:
        return shared_count + 2
    if shared_count < 2:
        return 0
    coverage = shared_count / max(1, len(proposition_terms))
    if coverage < 0.5:
        return 0
    return shared_count


def _grounding_subjects_compatible(
    proposition: str,
    evidence_clause: str,
) -> bool:
    """Keep the relation's actor aligned, not merely its shared entity.

    User utterances often omit an explicit ``我`` (``目前住在北京``), so an
    absent subject remains acceptable.  An *explicit* pet, friend, relative or
    third-person pronoun is different: it cannot ground a proposition whose
    relation subject is the user.  Propositions that preserve the third party
    (``用户的猫喜欢西瓜``) remain valid.
    """
    if not _candidate_relation_subject_is_user(proposition):
        return True
    normalized_evidence = _strip_memory_directive_prefix(evidence_clause)
    if _JOINT_FIRST_PERSON_SUBJECT_RE.search(normalized_evidence):
        return True
    return _THIRD_PARTY_EVIDENCE_SUBJECT_RE.search(normalized_evidence) is None


def _candidate_relation_subject_is_user(proposition: str) -> bool:
    if _CANDIDATE_THIRD_PARTY_SUBJECT_RE.search(proposition):
        return False
    return _DIRECT_USER_SUBJECT_RE.search(proposition) is not None


def _strip_memory_directive_prefix(text: str) -> str:
    stripped = re.sub(
        r"^\s*(?:(?:请|请你|麻烦你?|务必|一定要|帮我|你要|你得)\s*)?"
        r"(?:以后\s*)?(?:记住|记得|别忘(?:了)?|不要忘(?:了)?)\s*[,，:：]?\s*",
        "",
        text,
    )
    return re.sub(
        r"^\s*(?:(?:please\s+)|(?:(?:can|could|would|will)\s+you\s+))?"
        r"(?:remember|do\s+not\s+forget|don't\s+forget)\s*[,,:]?\s*",
        "",
        stripped,
        flags=re.IGNORECASE,
    )


def _relation_is_bound_to_asserted_object(
    relation: str,
    *,
    evidence_clause: str,
    proposition_terms: set[str],
    proposition_structured_values: set[tuple[str, str]],
) -> bool:
    patterns = _BOUND_RELATION_ASSERTION_PATTERNS.get(relation, ())
    for pattern in patterns:
        for match in re.finditer(pattern, evidence_clause, re.IGNORECASE):
            assertion = match.group(0)
            if proposition_terms & _grounding_terms(assertion):
                return True
            if any(
                _structured_value_present(value, assertion)
                for _, value in proposition_structured_values
            ):
                return True
    return False


def _grounding_relation_families(text: str) -> set[str]:
    return {
        family
        for family, patterns in _GROUNDING_RELATION_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }


def _grounding_has_negation(text: str) -> bool:
    # "not only" / "不但" are additive constructions rather than negative
    # polarity.  Strip them before applying the shared conservative detector.
    normalized = re.sub(
        r"不但|不仅|不只是|不只|\bnot\s+only\b|\bwithout\s+fail\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return _has_negation(normalized)


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

    memory_sensitive, memory_private = detected_sensitive_categories(candidate.memory)
    category_clauses: list[str] = []
    for clause in clauses:
        clause_sensitive, clause_private = detected_sensitive_categories(clause)
        if (memory_sensitive & clause_sensitive) or (memory_private & clause_private):
            category_clauses.append(clause)
    return category_clauses or [quote]


def _has_explicit_memory_directive(text: str) -> bool:
    """Recognize an imperative request, not a narrative such as ``I remember``."""
    return bool(
        _CHINESE_MEMORY_DIRECTIVE_PATTERN.search(text)
        or _ENGLISH_MEMORY_DIRECTIVE_PATTERN.search(text)
    )


def _has_scoped_explicit_memory_marker(message: str, quote: str) -> bool:
    """Require sensitive-memory authorization next to the quoted fact."""
    if _has_explicit_memory_directive(quote):
        return True
    for clause, previous_clause, next_clause in _clauses_around_quote(message, quote):
        if _has_explicit_memory_directive(clause):
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
        r"(?:please\s+)?(?:remember|do\s+not\s+forget|don't forget)(?:\s+this)?",
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
