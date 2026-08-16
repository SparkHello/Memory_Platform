"""/memories routes: evaluation."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_embedding_client, get_user_id
from app.api.memories.common import RecallEvalLabelsRequest, RecallEvalRunRequest
from app.config import Settings, get_settings
from app.memory.evaluation import (
    EvaluationError,
    build_recall_workbench,
    init_eval,
    run_diagnosis,
    run_recall_eval,
    save_labels,
)
from app.memory.search import EmbeddingClient, NullEmbeddingClient


router = APIRouter()

@router.get("/evaluation/diagnosis")
def memory_evaluation_diagnosis(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    result = run_diagnosis(
        settings.database_path,
        user_id=user_id,
        eval_dir=settings.eval_dir,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(result["error"]),
        )
    return result

@router.post("/evaluation/recall/init")
def memory_evaluation_recall_init(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    result = init_eval(
        source_db=settings.database_path,
        eval_dir=settings.eval_dir,
        user_id=user_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(result["error"]),
        )
    return result

@router.get("/evaluation/recall/workbench")
def memory_evaluation_recall_workbench(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    redact_sensitive: bool = Query(default=True),
) -> dict:
    try:
        return build_recall_workbench(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            redact_sensitive=redact_sensitive,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@router.put("/evaluation/recall/labels")
def memory_evaluation_recall_labels(
    body: RecallEvalLabelsRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return save_labels(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            labels=[label.model_dump() for label in body.labels],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@router.post("/evaluation/recall/run")
def memory_evaluation_recall_run(
    body: RecallEvalRunRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
) -> dict:
    if body.mode == "embedding" and isinstance(embedding_client, NullEmbeddingClient):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未配置 embedding 服务，无法运行语义召回评测",
        )
    try:
        return run_recall_eval(
            eval_dir=settings.eval_dir,
            user_id=user_id,
            mode=body.mode,
            k=body.k,
            embedding_client=embedding_client,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
