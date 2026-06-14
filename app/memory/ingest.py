import json

from app.llm.client import OpenAICompatibleClient
from app.memory.extractor import LLMMemoryExtractor
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
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.llm_client = llm_client

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
            )

        extractor = LLMMemoryExtractor(llm_client=self.llm_client)
        batch = await extractor.extract_many(
            source_text=source_text,
            assistant_message=assistant_message,
        )
        if not batch.outcomes:
            item = MemoryIngestItemResult(action="ignore", reason=batch.reason)
            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=_decision_log_json(
                    source=source,
                    payload={"action": "ignore", "reason": batch.reason},
                ),
                decision="ignore",
                reason=batch.reason,
            )
            return MemoryIngestResult(
                ignored=1,
                items=[item],
                reason=batch.reason,
            )

        resolver = MemoryResolver(store=self.store, embedding_client=self.embedding_client)
        items: list[MemoryIngestItemResult] = []
        created = updated = ignored = 0

        for outcome in batch.outcomes:
            candidate_log_json = _decision_log_json(
                source=source,
                payload=_json_payload(outcome.candidate_json),
            )
            if not outcome.accepted or outcome.candidate is None:
                ignored += 1
                self.store.create_decision_log(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    candidate_json=candidate_log_json,
                    decision="ignore",
                    reason=outcome.reason,
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

            self.store.create_decision_log(
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=candidate_log_json,
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
