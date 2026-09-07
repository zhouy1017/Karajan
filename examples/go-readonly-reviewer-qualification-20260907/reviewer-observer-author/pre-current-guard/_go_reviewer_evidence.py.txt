"""Native final-message selection and exact measured readonly input observations."""

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from karajan.adapters.opencode.go_evidence import DENIAL_PREFIX
from karajan.adapters.opencode.go_relay import GoReviewerQualificationContext
from karajan.candidates.review_output import MAX_OUTPUT_BYTES

from .go_probe import source_digest


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in value
    ):
        return "".join(part["text"] for part in value)
    return ""


def _exact_read(content: str, expected: bytes) -> bool:
    # OpenCode's pinned read tool renders each source line with its number. Do
    # not treat a quoted substring or a partial read as the complete file bytes.
    if content.count("<content>") != 1 or content.count("</content>") != 1:
        return False
    body = content.split("<content>", 1)[1].split("</content>", 1)[0]
    rows = re.findall(r"^(\d+): (.*)$", body, flags=re.MULTILINE)
    expected_lines = expected.decode("utf-8").splitlines()
    return rows == [(str(i), line) for i, line in enumerate(expected_lines, 1)]


@dataclass
class ReviewerRetention:
    files: dict[str, bytes] = field(repr=False)
    prompt: str = field(repr=False)
    canary: str = field(repr=False)
    previous: list[dict[str, Any]] = field(default_factory=list, repr=False)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, payload: dict[str, Any], measured: dict[str, Any]) -> None:
        messages = payload["messages"]
        paths = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                if function.get("name") == "read":
                    paths[call["id"]] = json.loads(function["arguments"]).get("filePath")
        reads = {}
        for message in messages:
            if message["role"] != "tool":
                continue
            native_path = paths.get(message.get("tool_call_id"))
            text = _content_text(message.get("content"))
            for path, expected in self.files.items():
                if native_path == "/workspace/" + path and _exact_read(text, expected):
                    reads[path] = {
                        "path": path,
                        "content_sha256": sha(expected),
                        "tool_result_sha256": sha(text.encode()),
                    }
        self.requests.append(
            {
                "sequence": len(self.requests) + 1,
                "request_digest": measured["request_digest"],
                "message_count": len(messages),
                "messages_digest": source_digest({"messages": messages}),
                "initial_input_retained": any(
                    m["role"] == "user" and self.prompt in _content_text(m.get("content"))
                    for m in messages
                ),
                "prior_messages_retained": messages[: len(self.previous)] == self.previous,
                "read_results": [reads[path] for path in sorted(reads)],
                "denied_canary_present": self.canary in json.dumps(payload),
            }
        )
        self.previous = copy.deepcopy(messages)

    def report(self) -> dict[str, Any]:
        return {
            "requests": copy.deepcopy(self.requests),
            "final_request_digest": self.requests[-1]["request_digest"] if self.requests else None,
        }


@dataclass(frozen=True)
class ObservedReviewerContext(GoReviewerQualificationContext):
    retention: ReviewerRetention = field(repr=False)

    def measure(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().measure(payload)
        self.retention.observe(payload, result)
        return result


def terminal(messages: list[dict[str, Any]]) -> bool:
    return any(
        m.get("info", {}).get("role") == "assistant"
        and (
            m["info"].get("error")
            or (
                m["info"].get("time", {}).get("completed")
                and m["info"].get("finish") not in {None, "tool-calls", "unknown"}
            )
        )
        for m in messages
    )


def select_final(
    messages: list[dict[str, Any]], session_id: str, prompt: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Only the native completed final text causally attached to this one prompt."""
    users = [m for m in messages if m.get("info", {}).get("role") == "user"]
    if len(users) != 1 or [
        p.get("text") for p in users[0].get("parts", []) if p.get("type") == "text"
    ] != [prompt]:
        raise ValueError("NATIVE_PROMPT_IDENTITY_MISMATCH")
    user = users[0]["info"]
    if user.get("sessionID") != session_id or not isinstance(user.get("id"), str):
        raise ValueError("NATIVE_PROMPT_IDENTITY_MISMATCH")
    assistants = [m for m in messages if m.get("info", {}).get("role") == "assistant"]
    if not assistants or any(
        m["info"].get("sessionID") != session_id
        or m["info"].get("parentID") != user["id"]
        or m["info"].get("providerID") != "opencode-go"
        or m["info"].get("modelID") != "glm-5.3-flash"
        or m["info"].get("error")
        for m in assistants
    ):
        raise ValueError("NATIVE_ASSISTANT_IDENTITY_MISMATCH")
    finals = [m for m in assistants if m["info"].get("finish") != "tool-calls"]
    if len(finals) != 1 or assistants[-1] is not finals[0]:
        raise ValueError("NATIVE_FINAL_AMBIGUOUS")
    final = finals[0]
    info = final["info"]
    completed = info.get("time", {}).get("completed")
    if (
        type(completed) not in {int, float}
        or not math.isfinite(completed)
        or completed <= 0
        or info.get("finish") != "stop"
    ):
        raise ValueError("NATIVE_FINAL_INCOMPLETE")
    texts = [p for p in final.get("parts", []) if p.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0].get("text"), str):
        raise ValueError("NATIVE_FINAL_TEXT_AMBIGUOUS")
    text_part = texts[0]
    started = text_part.get("time", {}).get("start")
    ended = text_part.get("time", {}).get("end")
    if (
        text_part.get("sessionID") != session_id
        or text_part.get("messageID") != info["id"]
        or not isinstance(text_part.get("id"), str)
        or type(started) not in {int, float}
        or type(ended) not in {int, float}
        or not math.isfinite(started)
        or not math.isfinite(ended)
        or started <= 0
        or ended < started
    ):
        raise ValueError("NATIVE_FINAL_TEXT_INCOMPLETE")
    text = text_part["text"]
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("REVIEW_OUTPUT_LIMIT_EXCEEDED")
    tools = []
    for message in assistants:
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state", {})
            name, path = part.get("tool"), state.get("input", {}).get("filePath")
            if name != "read" or state.get("status") not in {"completed", "error"}:
                raise ValueError("NATIVE_READ_TOOL_INCOMPLETE")
            paths = {"/workspace/" + p: p for p in ("acceptance.md", "src/range.py", "blocked.txt")}
            if path not in paths:
                raise ValueError("NATIVE_READ_TOOL_INCOMPLETE")
            error = str(state.get("error", ""))
            tools.append(
                {
                    "message_id": message["info"]["id"],
                    "call_id": part.get("callID"),
                    "name": name,
                    "path": "outside_projection"
                    if path == "/workspace/blocked.txt"
                    else paths[path],
                    "status": state["status"],
                    "permission_denied": error.startswith(DENIAL_PREFIX),
                    "os_not_found": error.startswith("File not found:"),
                }
            )
    return (
        {
            "session_id": session_id,
            "prompt_message_id": user["id"],
            "assistant_message_id": info["id"],
            "parent_id": info["parentID"],
            "provider_id": info["providerID"],
            "model_id": info["modelID"],
            "created_at": info.get("time", {}).get("created"),
            "completed_at": completed,
            "finish": info["finish"],
            "error": None,
            "text": text,
            "text_sha256": sha(text.encode()),
            "text_part_count": len(texts),
            "text_part_id": text_part["id"],
            "text_part_message_id": text_part["messageID"],
            "text_part_session_id": text_part["sessionID"],
            "text_part_started_at": started,
            "text_part_completed_at": ended,
        },
        tools,
    )
