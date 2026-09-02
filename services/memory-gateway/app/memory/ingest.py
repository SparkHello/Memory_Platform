import hashlib
import json

from app.llm.client import OpenAICompatibleClient
from app.memory.egress import (
    REASON_WITHHELD_ASSUMPTION_PREFIX,
    REASON_WITHHELD_LOCAL_SAVE_PREFIX,
    REASON_WITHHELD_NO_DIRECTIVE,
    SentenceSpan,
    local_directive_candidate,
    partition_for_egress,
    sentence_audit_fields,
    withheld_sentence_has_scoped_directive,
)
from app.memory.extractor import (
    ExtractionOutcome,
    LLMMemoryExtractor,
    _apply_sensitivity_floor,
    find_assumption_marker,
)
from app.memory.models import MemoryIngestItemResult, MemoryIngestResult
from app.memory.redaction import detect_text_sensitivity, higher_sensitivity
from app.memory.resolver import MemoryResolver
from app.memory.search import EmbeddingClient
from app.memory.store import MemoryStore


_MODEL_REASON_CODE_LABELS = {
    "has_candidates": "已提取候选",
    "no_long_term_value": "没有未来长期价值",
    "temporary_or_one_off": "临时状态或一次性事项",
    "hypothetical_or_uncertain": "假设或不确定表述",
    "not_user_asserted": "不是用户亲口陈述",
    "sensitive_without_explicit_request": "敏感信息未获明确记忆授权",
    "insufficient_context": "上下文不足",
    "other": "其他原因",
    "unclassified": "模型未提供有效原因码",
    "invalid_model_output": "模型输出格式无效",
    "upstream_unavailable": "提取模型不可用",
}


class MemoryIngestService:
    """Turn raw user text into validated, deduplicated long-term memories."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedding_client: EmbeddingClient,
        llm_client: OpenAICompatibleClient,
        allow_sensitive_egress: bool = False,
        egress_ceiling: str = "private",
        auto_supersede: bool = True,
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.llm_client = llm_client
        self.allow_sensitive_egress = allow_sensitive_egress
        # Highest local tier that may still reach the extraction model while
        # allow_sensitive_egress is False; sentences above it stay local.
        self.egress_ceiling = egress_ceiling
        self.auto_supersede = auto_supersede

    async def ingest(
        self,
        *,
        user_id: str,
        text: str,
        conversation_id: str | None = None,
        assistant_message: str | None = None,
        conversation_context: str | None = None,
        context_quote_source: str | None = None,
        source: str = "ingest",
    ) -> MemoryIngestResult:
        source_text = text.strip()
        if not source_text:
            return MemoryIngestResult(
                ignored=1,
                items=[
                    MemoryIngestItemResult(
                        action="ignore",
                        reason="提交文本为空",
                    )
                ],
                reason="提交文本为空",
                status="rejected",
            )

        # Sentence-level egress: only sentences above the ceiling stay local;
        # the rest of the message is still extracted. The filtered text is both
        # the model input and the grounding superset for source_quote checks.
        withheld: list[SentenceSpan] = []
        kept_count = 0
        extraction_source_text = source_text
        if not self.allow_sensitive_egress:
            partition = partition_for_egress(source_text, ceiling=self.egress_ceiling)
            withheld = partition.withheld
            kept_count = len(partition.kept)
            extraction_source_text = partition.egress_text

        resolver = MemoryResolver(
            store=self.store,
            embedding_client=self.embedding_client,
            auto_supersede=self.auto_supersede,
        )
        items: list[MemoryIngestItemResult] = []
        created = updated = ignored = 0

        if withheld:
            local_items, local_counts = await self._save_withheld_sentences(
                user_id=user_id,
                conversation_id=conversation_id,
                source=source,
                withheld=withheld,
                kept_count=kept_count,
                resolver=resolver,
            )
            items.extend(local_items)
            created += local_counts[0]
            updated += local_counts[1]
            ignored += local_counts[2]

        if not extraction_source_text:
            # Every sentence was withheld: nothing may reach the model.
            saved_locally = bool(created or updated)
            return MemoryIngestResult(
                created=created,
                updated=updated,
                ignored=ignored,
                items=items,
                reason=(
                    REASON_WITHHELD_LOCAL_SAVE_PREFIX
                    if saved_locally
                    else REASON_WITHHELD_NO_DIRECTIVE
                ),
                status="completed" if saved_locally else "rejected",
            )

        extraction_assistant_message = assistant_message
        extraction_conversation_context = conversation_context
        extraction_context_quote_source = context_quote_source
        if (
            not self.allow_sensitive_egress
            and assistant_message
            and detect_text_sensitivity(assistant_message) != "normal"
        ):
            # The user's source can still be safely extracted without copying a
            # sensitive model/tool result to a separate extraction provider.
            extraction_assistant_message = None
        if (
            not self.allow_sensitive_egress
            and conversation_context
            and detect_text_sensitivity(conversation_context) != "normal"
        ):
            # Callers should already filter context block-by-block. This
            # defense-in-depth check prevents mixed sensitive history from
            # reaching a separate extraction provider.
            extraction_conversation_context = None
            extraction_context_quote_source = None
        if (
            not self.allow_sensitive_egress
            and context_quote_source
            and detect_text_sensitivity(context_quote_source) != "normal"
        ):
            extraction_context_quote_source = None

        extractor = LLMMemoryExtractor(
            llm_client=self.llm_client,
            user_id=user_id,
        )
        batch = await extractor.extract_many(
            source_text=extraction_source_text,
            assistant_message=extraction_assistant_message,
            conversation_context=extraction_conversation_context,
            context_quote_source=extraction_context_quote_source,
        )
        if batch.retryable_error:
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={
                        "action": "ignore",
                        "error_code": batch.error_code,
                        "retryable": True,
                    },
                ),
                decision="ignore",
                reason=batch.reason,
            )
            # Local directive saves (if any) are already persisted; a retry
            # re-runs them idempotently through the resolver's dedup.
            return MemoryIngestResult(
                created=created,
                updated=updated,
                ignored=ignored,
                items=items,
                reason=batch.reason,
                status="retryable_error",
                retryable=True,
            )
        if not batch.outcomes:
            item = MemoryIngestItemResult(action="ignore", reason=batch.reason)
            model_reason_code = batch.reason_code
            model_reason_label = _MODEL_REASON_CODE_LABELS[model_reason_code]
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={
                        "action": "ignore",
                        "model_reason_code": model_reason_code,
                        **_text_audit_fields("model_reason", batch.reason),
                        "model_reason_redacted": True,
                    },
                ),
                decision="ignore",
                reason=(
                    "提取模型未返回候选记忆；"
                    f"原因码={model_reason_code}（{model_reason_label}）；"
                    "模型理由已脱敏"
                ),
            )
            items.append(item)
            return MemoryIngestResult(
                created=created,
                updated=updated,
                ignored=ignored + 1,
                items=items,
                reason=batch.reason,
                status="completed" if created or updated else "no_memory",
            )

        for outcome in batch.outcomes:
            candidate_log_payload = _candidate_audit_payload(outcome)
            if not outcome.accepted or outcome.candidate is None:
                ignored += 1
                self.store.create_decision_log(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    candidate_json=_decision_log_json(
                        source=source,
                        payload=candidate_log_payload,
                    ),
                    decision="ignore",
                    reason=_candidate_audit_reason(outcome),
                )
                items.append(
                    MemoryIngestItemResult(
                        action="ignore",
                        reason=outcome.reason,
                        content=outcome.candidate.memory if outcome.candidate else None,
                    )
                )
                continue

            result = await resolver.resolve(
                user_id=user_id,
                candidate=outcome.candidate,
                source_message=outcome.candidate.source_quote,
                conversation_id=conversation_id,
            )
            if result.action == "create":
                created += 1
            elif result.action == "update":
                updated += 1
            else:
                ignored += 1

            if result.memory is not None:
                candidate_log_payload["memory_id"] = result.memory.id
            if result.superseded_memory_id:
                # target_memory_id is a known decision-log reference key, so
                # purge scrubbing and health checks follow the closed row too.
                candidate_log_payload["target_memory_id"] = result.superseded_memory_id
                candidate_log_payload["relation"] = result.relation
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload=candidate_log_payload,
                ),
                decision=result.action,
                reason=result.reason,
            )
            items.append(
                MemoryIngestItemResult(
                    action=result.action,
                    relation=result.relation,
                    reason=result.reason,
                    memory_id=result.memory.id if result.memory else None,
                    content=result.memory.content if result.memory else outcome.candidate.memory,
                    superseded_memory_id=result.superseded_memory_id,
                )
            )

        return MemoryIngestResult(
            created=created,
            updated=updated,
            ignored=ignored,
            items=items,
            reason=batch.reason,
            status="completed" if created or updated else "rejected",
        )

    async def _save_withheld_sentences(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        source: str,
        withheld: list[SentenceSpan],
        kept_count: int,
        resolver: MemoryResolver,
    ) -> tuple[list[MemoryIngestItemResult], tuple[int, int, int]]:
        """Handle sentences that must not leave the host.

        A sentence with an explicit, clause-adjacent 记住 directive is saved
        verbatim through the resolver (dedup, store floor, private space, no
        remote embedding). Everything else is dropped and recorded as hashes.
        """
        items: list[MemoryIngestItemResult] = []
        created = updated = ignored = 0
        dropped: list[dict[str, object]] = []

        for span in withheld:
            audit = sentence_audit_fields(span)
            if not withheld_sentence_has_scoped_directive(span.text):
                ignored += 1
                items.append(
                    MemoryIngestItemResult(action="ignore", reason=REASON_WITHHELD_NO_DIRECTIVE)
                )
                dropped.append({**audit, "directive": False})
                continue

            marker = find_assumption_marker(span.text)
            if marker:
                ignored += 1
                reason = f"{REASON_WITHHELD_ASSUMPTION_PREFIX}（命中「{marker}」），不保存"
                items.append(MemoryIngestItemResult(action="ignore", reason=reason))
                dropped.append({**audit, "directive": True, "assumption": True})
                continue

            candidate = local_directive_candidate(span)
            _apply_sensitivity_floor(candidate)
            result = await resolver.resolve(
                user_id=user_id,
                candidate=candidate,
                source_message=span.stripped,
                conversation_id=conversation_id,
            )
            if result.action == "create":
                created += 1
            elif result.action == "update":
                updated += 1
            else:
                ignored += 1

            payload: dict[str, object] = {
                "action": result.action,
                "sensitive_egress_blocked": True,
                "local_directive_save": True,
                "sensitivity": candidate.sensitivity,
                "categories": span.categories,
                **_text_audit_fields("memory", candidate.memory),
                **_text_audit_fields("source_quote", span.stripped),
                "redacted": True,
            }
            if result.memory is not None:
                payload["memory_id"] = result.memory.id
            reason = f"{REASON_WITHHELD_LOCAL_SAVE_PREFIX}；{result.reason}"
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(source=source, payload=payload),
                decision=result.action,
                reason=reason,
            )
            items.append(
                MemoryIngestItemResult(
                    action=result.action,
                    relation=result.relation,
                    reason=reason,
                    memory_id=result.memory.id if result.memory else None,
                    content=result.memory.content if result.memory else candidate.memory,
                )
            )

        if dropped:
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={
                        "action": "ignore",
                        "sensitive_egress_blocked": True,
                        "withheld_sentence_count": len(withheld),
                        "kept_sentence_count": kept_count,
                        "withheld_sentences": dropped,
                        "redacted": True,
                    },
                ),
                decision="ignore",
                reason=REASON_WITHHELD_NO_DIRECTIVE,
            )
        return items, (created, updated, ignored)


def _json_payload(raw_json: str) -> dict:
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"raw": raw_json[:500]}
    return data if isinstance(data, dict) else {"raw": raw_json[:500]}


def _decision_log_json(*, source: str, payload: dict) -> str:
    return json.dumps({"source": source, **payload}, ensure_ascii=False)


def _candidate_audit_payload(outcome: ExtractionOutcome) -> dict:
    """Keep decision logs useful without duplicating quoted sensitive text."""
    payload = _json_payload(outcome.candidate_json)
    candidate = outcome.candidate

    quote = payload.pop("source_quote", None)
    if isinstance(quote, str) and quote:
        payload.update(_text_audit_fields("source_quote", quote))
        payload["source_quote_redacted"] = True
    context_quote = payload.pop("context_quote", None)
    if isinstance(context_quote, str) and context_quote:
        payload.update(_text_audit_fields("context_quote", context_quote))
        payload["context_quote_redacted"] = True

    if candidate is None:
        raw_output = payload.pop("raw", None)
        if isinstance(raw_output, str) and raw_output:
            payload.update(_text_audit_fields("raw_output", raw_output))
            payload["redacted"] = True
            return payload

        detected = detect_text_sensitivity(
            json.dumps(payload, ensure_ascii=False, default=str)
        )
        declared = str(payload.get("sensitivity") or "normal")
        if declared != "normal" or detected != "normal":
            memory = str(payload.pop("memory", "") or "")
            payload.pop("reason", None)
            payload.pop("entities", None)
            payload.pop("topics", None)
            if memory:
                payload.update(_text_audit_fields("memory", memory))
            payload["sensitivity"] = higher_sensitivity(declared, detected)
            payload["redacted"] = True
        return payload

    detected = detect_text_sensitivity(
        "\n".join(
            part
            for part in (
                candidate.source_quote,
                candidate.memory,
                candidate.reason,
                *candidate.entities,
            )
            if part
        )
    )
    if candidate.sensitivity == "normal" and detected == "normal":
        return payload

    memory = str(payload.pop("memory", candidate.memory) or "")
    payload.pop("reason", None)
    payload.pop("entities", None)
    payload.pop("topics", None)
    payload.update(_text_audit_fields("memory", memory))
    payload["sensitivity"] = higher_sensitivity(candidate.sensitivity, detected)
    payload["redacted"] = True
    return payload


def _candidate_audit_reason(outcome: ExtractionOutcome) -> str:
    candidate = outcome.candidate
    if candidate is not None:
        detected = detect_text_sensitivity(
            "\n".join(
                part
                for part in (candidate.source_quote, candidate.memory, candidate.reason)
                if part
            )
        )
        if candidate.sensitivity != "normal" or detected != "normal":
            return "敏感候选未保存；详细理由已脱敏"
    return outcome.reason


def _text_audit_fields(prefix: str, text: str) -> dict[str, int | str]:
    return {
        f"{prefix}_length": len(text),
        f"{prefix}_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
