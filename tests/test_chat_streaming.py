from app.openai_compat.streaming import (
    ChatStreamCapture,
    extract_non_stream_result,
    extract_non_stream_tool_trace,
)


def test_stream_capture_handles_split_events_usage_and_done() -> None:
    capture = ChatStreamCapture()
    capture.feed(b'data: {"choices":[{"delta":{"content":"hel')
    capture.feed(b'lo"},"finish_reason":null}]}\r\n\r\n')
    capture.feed(
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
    )
    capture.feed(
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    capture.finish(clean=True)

    assert capture.assistant_text == "hello"
    assert capture.saw_done is True
    assert capture.is_final_text_response is True
    assert capture.usage == {"prompt_tokens": 1, "completion_tokens": 1}


def test_stream_capture_keeps_final_reasoning_for_completed_tool_turns() -> None:
    capture = ChatStreamCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"final plan",'
        b'"content":"answer"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    assert capture.final_text_trace_ready is True
    assert capture.assistant_reasoning == "final plan"
    capture.finish(clean=True)
    assert capture.is_final_text_response is True


def test_stream_capture_rejects_tools_truncation_and_disconnects() -> None:
    tool_capture = ChatStreamCapture()
    tool_capture.feed(
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]},"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    tool_capture.finish(clean=True)

    length_capture = ChatStreamCapture()
    length_capture.feed(
        b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    length_capture.finish(clean=True)

    disconnected = ChatStreamCapture()
    disconnected.feed(
        b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}\n\n'
    )
    disconnected.finish(clean=False)

    assert tool_capture.is_final_text_response is False
    assert length_capture.is_final_text_response is False
    assert disconnected.is_final_text_response is False


def test_non_stream_capture_preserves_final_only_contract() -> None:
    text, final = extract_non_stream_result(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "not ingested",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    truncated_text, truncated_final = extract_non_stream_result(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial"},
                    "finish_reason": "length",
                }
            ]
        }
    )

    assert (text, final) == ("answer", True)
    assert (truncated_text, truncated_final) == ("", False)


def test_stream_capture_overflow_disables_ingest_without_needing_more_memory() -> None:
    malformed = ChatStreamCapture(max_event_buffer_bytes=16)
    malformed.feed(b"x" * 17)

    long_text = ChatStreamCapture(max_text_chars=4)
    long_text.feed(
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    long_text.finish(clean=True)

    assert malformed.capture_overflowed is True
    assert len(malformed._buffer) == 0
    assert malformed.is_final_text_response is False
    assert long_text.capture_overflowed is True
    assert long_text.assistant_text == ""
    assert long_text.is_final_text_response is False


def test_stream_error_event_prevents_partial_answer_ingest() -> None:
    capture = ChatStreamCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}\n\n'
        b'data: {"error":{"message":"upstream failed"}}\n\n'
        b"data: [DONE]\n\n"
    )
    capture.finish(clean=True)

    assert capture.saw_error is True
    assert capture.is_final_text_response is False


def test_stream_capture_preserves_reasoning_for_flit_tool_history() -> None:
    capture = ChatStreamCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"plan "},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning_content":"carefully",'
        b'"tool_calls":[{"index":0,"id":"call_1","type":"function"}]},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert capture.tool_call_trace_ready is True
    capture.finish(clean=True)

    assert capture.assistant_reasoning == "plan carefully"
    assert capture.tool_call_ids == ["call_1"]
    assert capture.is_complete_tool_call_response is True
    assert capture.is_final_text_response is False


def test_non_stream_tool_trace_preserves_reasoning_and_ids() -> None:
    reasoning, tool_call_ids, complete = extract_non_stream_tool_trace(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private provider state",
                        "tool_calls": [
                            {"id": "call_1", "type": "function"},
                            {"id": "call_2", "type": "function"},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )

    assert reasoning == "private provider state"
    assert tool_call_ids == ["call_1", "call_2"]
    assert complete is True


def test_stream_capture_ignores_data_after_done() -> None:
    capture = ChatStreamCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"content":"visible"},'
        b'"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
        b'data: {"choices":[{"delta":{"content":"hidden"},'
        b'"finish_reason":"stop"}]}\n\n'
    )
    capture.finish(clean=True)

    assert capture.assistant_text == "visible"
    assert capture.is_final_text_response is True


def test_stream_capture_handles_json_and_done_in_one_sse_event() -> None:
    capture = ChatStreamCapture()
    capture.feed(
        b'data: {"choices":[{"delta":{"content":"answer"},'
        b'"finish_reason":"stop"}]}\n'
        b"data: [DONE]\n\n"
    )
    capture.finish(clean=True)

    assert capture.assistant_text == "answer"
    assert capture.saw_done is True
    assert capture.is_final_text_response is True


def test_stream_capture_rejects_malformed_json_before_done() -> None:
    capture = ChatStreamCapture()
    capture.feed(b"data: not-json\n\n")
    capture.feed(
        b'data: {"choices":[{"delta":{"content":"answer"},'
        b'"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    capture.finish(clean=True)

    assert capture.saw_error is True
    assert capture.is_final_text_response is False


def test_stream_capture_caps_unique_tool_call_ids() -> None:
    capture = ChatStreamCapture(max_tool_call_ids=2)
    for index in range(3):
        capture.feed(
            (
                'data: {"choices":[{"delta":{"tool_calls":['
                f'{{"index":{index},"id":"call_{index}"}}'
                ']},"finish_reason":null}]}\n\n'
            ).encode()
        )

    assert capture.capture_overflowed is True
    assert capture.tool_call_ids == []
