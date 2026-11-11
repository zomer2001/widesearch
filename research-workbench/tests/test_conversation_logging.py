import json

from langchain_core.messages import HumanMessage

from searchos.harness.telemetry.conversation import (
    ConversationLogger,
    langchain_msg_to_conversation_msgs,
)
from searchos.harness.telemetry.trajectory import ConversationMessage


def test_snapshot_is_pretty_printed_and_keeps_unicode(tmp_path):
    logger = ConversationLogger(tmp_path / "conversation.json")
    logger.log(ConversationMessage(role="user", content="第一行\n第二行"))
    logger.flush()

    path = tmp_path / "conversations/snapshots/orchestrator.json"
    raw = path.read_text(encoding="utf-8")

    assert raw.startswith('{\n  "agent_name": "orchestrator",')
    assert '\n  "messages": [\n' in raw
    assert "第一行" in raw
    assert "\\u7b2c" not in raw
    assert raw.endswith("\n")
    assert json.loads(raw)["messages"][0]["content"] == "第一行\n第二行"


def test_structured_message_content_stays_json_instead_of_python_repr(tmp_path):
    blocks = [
        {"type": "text", "text": "查看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]

    converted = langchain_msg_to_conversation_msgs(HumanMessage(content=blocks))

    assert converted[0].content == blocks
    dumped = converted[0].model_dump()
    assert isinstance(dumped["content"], list)
    assert dumped["content"][0]["text"] == "查看这张图"

    logger = ConversationLogger(tmp_path / "conversation.json")
    logger.log(converted[0])
    logger.flush()
    snapshot = json.loads(
        (tmp_path / "conversations/snapshots/orchestrator.json").read_text(encoding="utf-8")
    )
    assert snapshot["messages"][0]["content"] == blocks


def test_generated_context_keeps_its_synthetic_marker_in_conversation():
    converted = langchain_msg_to_conversation_msgs(HumanMessage(
        content="Generated goal packet",
        additional_kwargs={"sf_synthetic": True, "sf_kind": "p2_context"},
    ))
    assert converted[0].metadata == {"synthetic": "p2_context"}
    assert langchain_msg_to_conversation_msgs(HumanMessage(content="Real question"))[0].metadata == {}


def test_history_ignores_generated_context_and_preserves_real_followups(tmp_path):
    from searchos.harness.telemetry.conversation_context import conversation_turns

    snapshot = tmp_path / "conversations/snapshots/orchestrator.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({"messages": [
        {"role": "user", "content": "Compare the two notices"},
        # Historical conversion dropped this packet's synthetic metadata.
        {"role": "user", "content": (
            "[P2 GOAL — authoritative user text; keep separate from history summary]\n"
            "Original objective: Compare the two notices"
        )},
        {"role": "user", "content": "Generated context in any future format",
         "metadata": {"synthetic": "p2_context"}},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": (
            "## 对话历史（供参考；本轮已沿用上一轮的覆盖表/证据）\nPrior answer\n"
            "---\n当前问题：Include the full effective dates"
        )},
        {"role": "user", "content": "[Context handoff — historical notes, not new evidence]\nNotes"},
        {"role": "user", "content": "[用户追问]\nUse both sources",
         "metadata": {"steer_text": "Use both sources"}},
        {"role": "assistant", "content": "Second answer"},
    ]}), encoding="utf-8")
    assert conversation_turns(tmp_path) == [
        {"query": "Compare the two notices", "answer": "First answer", "steers": []},
        {"query": "Include the full effective dates", "answer": "Second answer",
         "steers": ["Use both sources"]},
    ]
