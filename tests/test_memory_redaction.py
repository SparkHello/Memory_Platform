"""redaction 模块的本地敏感检测与脱敏行为测试。

detect_text_sensitivity / sensitivity_floor 是确定性本地安全下限，
redact_memory_payload 是敏感内容的响应遮罩。两者都是安全关键路径，
不能依赖 LLM 或网络。
"""

from app.memory.redaction import (
    REDACTED_CONTENT_TEXT,
    REDACTED_SOURCE_TEXT,
    detect_text_sensitivity,
    detected_sensitive_categories,
    redact_memory_payload,
    sensitivity_floor,
)


class TestDetectTextSensitivity:
    def test_normal_text_is_not_flagged(self) -> None:
        assert detect_text_sensitivity("我喜欢黑咖啡") == "normal"
        assert detect_text_sensitivity("下周三开会讨论项目排期") == "normal"

    def test_contact_information_is_private(self) -> None:
        assert detect_text_sensitivity("我的邮箱是 user@example.com") == "private"
        assert detect_text_sensitivity("手机号 13800138000") == "private"

    def test_credentials_are_sensitive(self) -> None:
        assert detect_text_sensitivity("银行卡密码是 123456") == "sensitive"
        assert detect_text_sensitivity("API key 是 sk-abc123def456") == "sensitive"

    def test_health_facts_are_sensitive(self) -> None:
        assert detect_text_sensitivity("需要持续控制血糖") == "sensitive"

    def test_sensitive_wins_over_private(self) -> None:
        text = "我的银行卡号 6222020200001234567，邮箱 user@example.com"
        assert detect_text_sensitivity(text) == "sensitive"

    def test_salary_is_private_finance(self) -> None:
        assert detect_text_sensitivity("我的工资是两万") == "private"
        assert detect_text_sensitivity("每月收入两万") == "private"

    def test_english_patterns(self) -> None:
        assert detect_text_sensitivity("my email is user@example.com") == "private"
        assert detect_text_sensitivity("the password is hunter2hunter2") == "sensitive"
        assert detect_text_sensitivity("I drink coffee") == "normal"


class TestDetectedSensitiveCategories:
    def test_returns_category_sets(self) -> None:
        sensitive, private = detected_sensitive_categories("银行卡密码是 123456")
        assert "credential" in sensitive
        assert "financial_account" in sensitive
        assert private == set()

    def test_contact_category(self) -> None:
        sensitive, private = detected_sensitive_categories("邮箱 user@example.com")
        assert sensitive == set()
        assert "contact" in private


class TestSensitivityFloor:
    def test_declared_normal_raised_to_detected(self) -> None:
        assert sensitivity_floor("normal", "银行卡密码是 123456") == "sensitive"
        assert sensitivity_floor("normal", "邮箱 user@example.com") == "private"

    def test_declared_private_raised_to_sensitive(self) -> None:
        assert sensitivity_floor("private", "密码是 hunter2hunter2") == "sensitive"

    def test_declared_higher_than_detected_stays(self) -> None:
        assert sensitivity_floor("sensitive", "我喜欢咖啡") == "sensitive"

    def test_multiple_texts_joined(self) -> None:
        assert sensitivity_floor("normal", "我喜欢咖啡", "邮箱 a@b.com") == "private"

    def test_none_texts_ignored(self) -> None:
        assert sensitivity_floor("normal", None, "我喜欢咖啡", None) == "normal"


class TestRedactMemoryPayload:
    def test_redact_disabled_returns_copy(self) -> None:
        payload = {"id": "m1", "content": "secret", "sensitivity": "sensitive"}
        result = redact_memory_payload(payload, redact_sensitive=False)
        assert result == payload
        assert result is not payload

    def test_sensitive_payload_is_redacted(self) -> None:
        payload = {
            "id": "m1",
            "content": "银行卡密码是 123456",
            "source_message": "记住，银行卡密码是 123456",
            "source_excerpt": "密码是 123456",
            "sensitivity": "sensitive",
        }
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["content"] == REDACTED_CONTENT_TEXT
        assert result["source_message"] == REDACTED_SOURCE_TEXT
        assert result["source_excerpt"] == REDACTED_SOURCE_TEXT
        assert result["redacted"] is True
        assert result["redaction_reason"] == "sensitive"
        assert set(result["redacted_fields"]) == {
            "content",
            "source_message",
            "source_excerpt",
        }

    def test_private_payload_is_redacted(self) -> None:
        payload = {"content": "邮箱 user@example.com", "sensitivity": "private"}
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["content"] == REDACTED_CONTENT_TEXT
        assert result["redacted"] is True

    def test_normal_payload_is_not_redacted(self) -> None:
        payload = {"content": "喜欢黑咖啡", "sensitivity": "normal"}
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["content"] == "喜欢黑咖啡"
        assert "redacted" not in result

    def test_local_detection_redacts_mislabeled_legacy_payload(self) -> None:
        payload = {
            "content": "银行卡密码是 123456",
            "source_message": "旧库错误地标成 normal",
            "label": "银行卡密码是 123456",
            "entities": ["123456"],
            "sensitivity": "normal",
        }

        result = redact_memory_payload(payload, redact_sensitive=True)

        assert result["content"] == REDACTED_CONTENT_TEXT
        assert result["source_message"] == REDACTED_SOURCE_TEXT
        assert result["label"] == "敏感记忆"
        assert result["entities"] == []
        assert result["redaction_reason"] == "sensitive"

    def test_missing_sensitivity_uses_payload_value(self) -> None:
        payload = {"content": "secret", "sensitivity": "sensitive"}
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["content"] == REDACTED_CONTENT_TEXT

    def test_explicit_sensitivity_overrides_payload(self) -> None:
        payload = {"content": "secret", "sensitivity": "normal"}
        result = redact_memory_payload(
            payload,
            redact_sensitive=True,
            sensitivity="sensitive",
        )
        assert result["content"] == REDACTED_CONTENT_TEXT
        assert result["redaction_reason"] == "sensitive"

    def test_missing_fields_are_not_invented(self) -> None:
        payload = {"id": "m1", "sensitivity": "sensitive"}
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["redacted"] is True
        assert "content" not in result
        assert result["redacted_fields"] == []

    def test_unknown_sensitivity_value_not_redacted(self) -> None:
        payload = {"content": "whatever", "sensitivity": "unknown"}
        result = redact_memory_payload(payload, redact_sensitive=True)
        assert result["content"] == "whatever"
        assert "redacted" not in result
