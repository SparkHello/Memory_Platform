from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_user_id, require_api_key
from app.config import Settings, get_settings
from app.usage.store import UsageStore


router = APIRouter(
    prefix="/usage",
    tags=["model usage"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/summary")
def model_usage_summary(
    user_id: Annotated[str, Depends(get_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    range: Annotated[
        Literal["7", "30", "90", "all"],
        Query(description="统计最近 7/30/90 天或全部历史"),
    ] = "30",
):
    days = None if range == "all" else int(range)
    return UsageStore(settings.database_path).summary(user_id=user_id, days=days)
