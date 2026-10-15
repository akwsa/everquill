"""Provider-neutral reply shapes; tool requests never grant local authority."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ToolCallRequest:
    name: str
    arguments: Mapping[str, object]
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str

    def __post_init__(self):
        if not isinstance(self.call_id, str) or not self.call_id or not isinstance(self.output, str):
            raise ValueError("invalid tool result")


@dataclass(frozen=True)
class BackendReply:
    status: str
    text: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    response_id: str | None = None

    def __post_init__(self):
        if self.response_id is not None and (not isinstance(self.response_id, str) or not self.response_id):
            raise ValueError("invalid backend response ID")
        if self.status == "complete":
            valid = isinstance(self.text, str) and bool(self.text) and not self.tool_calls
        elif self.status in ("timeout", "cancelled"):
            valid = self.text == "" and not self.tool_calls
        elif self.status == "tool_request":
            valid = isinstance(self.text, str) and bool(self.tool_calls) and all(
                isinstance(call, ToolCallRequest) and isinstance(call.name, str)
                and bool(call.name) and isinstance(call.arguments, Mapping)
                for call in self.tool_calls)
        else:
            valid = False
        if not valid:
            raise ValueError("invalid backend response")


def require_final_reply(reply):
    """Reject malformed/unsupported backend output before agent policy sees it."""
    if not isinstance(reply, BackendReply) or reply.status == "tool_request":
        raise ValueError("invalid backend response")
    return reply
