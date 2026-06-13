from datetime import UTC, datetime
import json
import re


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_json_object(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"```\s*$", "", stripped)
    for candidate_text in (stripped, _first_json_block(stripped)):
        if not candidate_text:
            continue
        try:
            data = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _first_json_block(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group() if match else None


def _term_jaccard(left: str, right: str) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", text)}


def _char_overlap(left: str, right: str) -> float:
    left_chars = {char.lower() for char in left if not char.isspace()}
    right_chars = {char.lower() for char in right if not char.isspace()}
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / len(left_chars | right_chars)


def _has_negation(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "不",
        "不是",
        "不再",
        "没有",
        "没",
        "停止",
        "戒掉",
        "讨厌",
        "不喜欢",
        "不喝",
        "no longer",
        "not",
    )
    return any(marker in lowered for marker in markers)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!?！？").lower()
