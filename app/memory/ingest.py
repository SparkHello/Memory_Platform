import hashlib
import json

from app.llm.client import OpenAICompatibleClient
from app.memory.extractor import (
    ExtractionOutcome,
    LLMMemoryExtractor,
    detect_text_sensitivity,
)
from app.memory.models import MemoryIngestItemResult, MemoryIngestResult
from app.memory.resolver import MemoryResolver
from app.memory.search import EmbeddingClient
from app.memory.store import MemoryStore


class MemoryIngestService:
    """Turn raw user text into validated, deduplicated long-term memories."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedding_client: EmbeddingClient,
        llm_client: OpenAICompatibleClient,
        allow_sensitive_egress: bool = False,
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.llm_client = llm_client
        self.allow_sensitive_egress = allow_sensitive_egress

    async def ingest(
        self,
        *,
        user_id: str,
        text: str,
        conversation_id: str | None = None,
        assistant_message: str | None = None,
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

        if not self.allow_sensitive_egress and detect_text_sensitivity(source_text) != "normal":
            reason = "敏感原文未发送给远程提取模型；如确需处理，请显式启用 ALLOW_SENSITIVE_EGRESS"
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={"action": "ignore", "sensitive_egress_blocked": True},
                ),
                decision="ignore",
                reason=reason,
            )
            return MemoryIngestResult(
                ignored=1,
                items=[MemoryIngestItemResult(action="ignore", reason=reason)],
                reason=reason,
                status="rejected",
            )

        extractor = LLMMemoryExtractor(llm_client=self.llm_client)
        batch = await extractor.extract_many(
            source_text=source_text,
            assistant_message=assistant_message,
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
            return MemoryIngestResult(
                reason=batch.reason,
                status="retryable_error",
                retryable=True,
            )
        if not batch.outcomes:
            item = MemoryIngestItemResult(action="ignore", reason=batch.reason)
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={
                        "action": "ignore",
                        **_text_audit_fields("model_reason", batch.reason),
                        "model_reason_redacted": True,
                    },
                ),
                decision="ignore",
                reason="提取模型未返回候选记忆；模型理由已脱敏",
            )
            return MemoryIngestResult(
                ignored=1,
                items=[item],
                reason=batch.reason,
                status="no_memory",
            )

        resolver = MemoryResolver(store=self.store, embedding_client=self.embedding_client)
        items: list[MemoryIngestItemResult] = []
        created = updated = ignored = 0

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
            payload["sensitivity"] = _higher_sensitivity(declared, detected)
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
    payload["sensitivity"] = _higher_sensitivity(candidate.sensitivity, detected)
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


def _higher_sensitivity(left: str, right: str) -> str:
    rank = {"normal": 0, "private": 1, "sensitive": 2}
    normalized_left = left if left in rank else "sensitive"
    normalized_right = right if right in rank else "sensitive"
    return max((normalized_left, normalized_right), key=rank.__getitem__)
