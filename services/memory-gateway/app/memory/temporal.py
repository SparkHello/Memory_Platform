from datetime import UTC, datetime, timedelta
import re
from typing import Literal

from app.memory.models import MemoryRecord
from app.memory.utils import _parse_iso_datetime, _utc_now


TemporalQueryMode = Literal["current", "history", "future"]

_HISTORY_QUERY_MARKERS = (
    "以前",
    "之前",
    "过去",
    "曾经",
    "当时",
    "原来",
    "从前",
    "历史上",
    "去年",
    "前年",
    "上个月",
    "previously",
    "formerly",
    "used to",
    "in the past",
    "last year",
    "at the time",
)
_FUTURE_QUERY_MARKERS = (
    "未来",
    "以后",
    "将来",
    "届时",
    "到时",
    "明年",
    "后年",
    "下个月",
    "即将",
    "将会",
    "预定",
    "future",
    "next year",
    "next month",
    "will be",
    "going to",
)
_CURRENT_QUERY_MARKERS = (
    "现在",
    "目前",
    "如今",
)
_CURRENT_QUERY_WORD_RE = re.compile(r"\b(?:now|currently|presently)\b")
_HISTORY_QUERY_RE = re.compile(
    r"(?:我|用户).{0,24}(?:什么时候|何时).{0,16}"
    r"(?:住(?:在|过)?|居住(?:在|过)?|工作(?:在|过)?|任职(?:于|过)?|"
    r"就职(?:于|过)?|使用过|拥有过|去过)"
    r"|(?:什么时候|何时).{0,16}(?:我|用户).{0,16}"
    r"(?:住(?:在|过)?|居住(?:在|过)?|工作(?:在|过)?|任职(?:于|过)?|"
    r"就职(?:于|过)?|使用过|拥有过|去过)"
    r"|(?:我|用户).{0,24}(?:住|居住|工作|任职|就职|使用|拥有|去|到|学习).{0,8}过"
    r"|\b(?:where|when|what|which|who|how)\b.{0,48}\bdid\s+(?:i|the\s+user)\b"
    r"|\bdid\s+(?:i|the\s+user)\b"
    r"|\b(?:where|when|what|which)\b.{0,48}\b(?:have|has)\s+"
    r"(?:i|the\s+user)\s+(?:ever\s+)?(?:lived|worked|used|owned|visited|studied|been)\b"
    r"|\bhave\s+i\s+(?:ever\s+)?(?:lived|worked|used|owned|visited|studied|been)\b",
    flags=re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:年)?(?!\d)")
_FULL_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:年|[-/.])\s*(\d{1,2})"
    r"\s*(?:月|[-/.])\s*(\d{1,2})\s*日?(?!\d)"
)
_MONTH_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*月?(?!\d)"
)


def temporal_query_mode(
    query: str,
    *,
    now: datetime | None = None,
) -> TemporalQueryMode:
    """Infer an explicit temporal intent while keeping ordinary queries current."""
    normalized = " ".join(query.casefold().split())
    # A historical/future phrase may describe when the answer will be used or
    # how it was remembered, while the embedded fact is explicitly about now:
    # e.g. “之前记住的我现在住哪里” or “以后回答时依据我现在住哪里”.
    if (
        any(marker in normalized for marker in _CURRENT_QUERY_MARKERS)
        or _CURRENT_QUERY_WORD_RE.search(normalized)
    ):
        return "current"
    if any(marker in normalized for marker in _HISTORY_QUERY_MARKERS):
        return "history"
    if any(marker in normalized for marker in _FUTURE_QUERY_MARKERS):
        return "future"
    if _HISTORY_QUERY_RE.search(normalized):
        return "history"

    current = _utc_now(now)
    years = [int(match.group(1)) for match in _YEAR_RE.finditer(normalized)]
    if years and max(years) < current.year:
        return "history"
    if years and min(years) > current.year:
        return "future"
    return "current"


def memory_matches_temporal_mode(
    memory: MemoryRecord,
    *,
    mode: TemporalQueryMode,
    now: datetime | None = None,
    query_window: tuple[datetime, datetime] | None = None,
) -> bool:
    """Return whether a temporal fact belongs to the requested time view.

    Memories without temporal metadata remain eligible in every view: an ordinary
    episodic memory can still answer a historical query even when it was not
    modeled as a versioned fact.
    """
    if not _has_temporal_metadata(memory):
        return True

    current = _utc_now(now)
    starts_at = _parse_iso_datetime(memory.valid_from)
    ends_at = _parse_iso_datetime(memory.valid_until)
    is_point_event = _is_point_event(memory)

    if query_window is not None:
        window_start, window_end = query_window
        starts_at = starts_at or _parse_iso_datetime(memory.created_at)
        if is_point_event:
            return (
                starts_at is not None
                and window_start <= starts_at < window_end
            )
        return (
            (starts_at is None or starts_at < window_end)
            and (ends_at is None or ends_at > window_start)
        )

    # A timestamped episodic fact is an event, not an open-ended state. Past
    # events remain eligible to ordinary recall and to explicit history views.
    if is_point_event:
        if mode == "future":
            return starts_at is not None and starts_at > current
        if mode == "history":
            return starts_at is None or starts_at <= current
        return starts_at is None or starts_at <= current

    if mode == "future":
        return starts_at is not None and starts_at > current

    if mode == "history":
        if starts_at is not None and starts_at > current:
            return False
        return (
            (ends_at is not None and ends_at <= current)
            or (bool(memory.superseded_by) and ends_at is None)
        )

    if starts_at is not None and starts_at > current:
        return False
    if ends_at is not None and ends_at <= current:
        return False
    # Legacy/corrupt supersession rows may lack the closing boundary. They must
    # not enter the current view. A scheduled replacement has a future end and
    # remains current until that instant.
    if memory.superseded_by and ends_at is None:
        return False
    return True


def is_current_temporal_memory(
    memory: MemoryRecord,
    *,
    now: datetime | None = None,
) -> bool:
    return memory_matches_temporal_mode(memory, mode="current", now=now)


def temporal_query_window(
    query: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """Return the explicit calendar window mentioned by a query, if any."""
    normalized = " ".join(query.casefold().split())
    current = _utc_now(now)
    relative_year: int | None = None
    if "前年" in normalized or "year before last" in normalized:
        relative_year = current.year - 2
    elif "去年" in normalized or "last year" in normalized:
        relative_year = current.year - 1
    elif "明年" in normalized or "next year" in normalized:
        relative_year = current.year + 1
    elif "后年" in normalized or "year after next" in normalized:
        relative_year = current.year + 2
    if relative_year is not None:
        return (
            datetime(relative_year, 1, 1, tzinfo=UTC),
            datetime(relative_year + 1, 1, 1, tzinfo=UTC),
        )

    relative_month: int | None = None
    if (
        "上个月" in normalized
        or "上月" in normalized
        or "last month" in normalized
        or "previous month" in normalized
    ):
        relative_month = -1
    elif (
        "下个月" in normalized
        or "下月" in normalized
        or "next month" in normalized
        or "following month" in normalized
    ):
        relative_month = 1
    if relative_month is not None:
        month_index = current.year * 12 + current.month - 1 + relative_month
        year_value, zero_based_month = divmod(month_index, 12)
        return _calendar_month_window(year_value, zero_based_month + 1)

    full_date = _FULL_DATE_RE.search(normalized)
    if full_date:
        try:
            start = datetime(
                int(full_date.group(1)),
                int(full_date.group(2)),
                int(full_date.group(3)),
                tzinfo=UTC,
            )
        except ValueError:
            return None
        return start, start + timedelta(days=1)

    month = _MONTH_RE.search(normalized)
    if month:
        year_value = int(month.group(1))
        month_value = int(month.group(2))
        try:
            return _calendar_month_window(year_value, month_value)
        except ValueError:
            return None

    year = _YEAR_RE.search(normalized)
    if year:
        year_value = int(year.group(1))
        return (
            datetime(year_value, 1, 1, tzinfo=UTC),
            datetime(year_value + 1, 1, 1, tzinfo=UTC),
        )
    return None


def _calendar_month_window(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        return start, datetime(year + 1, 1, 1, tzinfo=UTC)
    return start, datetime(year, month + 1, 1, tzinfo=UTC)


def _has_temporal_metadata(memory: MemoryRecord) -> bool:
    return bool(
        memory.valid_from
        or memory.valid_until
        or memory.temporal_subject
        or memory.temporal_predicate
        or memory.supersedes
        or memory.superseded_by
    )


def _is_point_event(memory: MemoryRecord) -> bool:
    return bool(
        memory.valid_from
        and not memory.valid_until
        and not (memory.temporal_subject and memory.temporal_predicate)
        and not memory.supersedes
        and not memory.superseded_by
    )
