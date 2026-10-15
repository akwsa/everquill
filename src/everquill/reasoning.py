"""Provider-independent request and final-reply boundary.

Local context consent, proposal validation and execution are not backend powers.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from everquill.reply import BackendReply


@dataclass(frozen=True)
class BackendCapabilities:
    streaming: bool
    tools: bool
    cancellation: bool
    sessions: bool
    max_context: int | None


class CancellationStatus(str, Enum):
    LOCAL_CANCELLED = "LOCAL_CANCELLED"
    SERVER_CONFIRMED = "SERVER_CONFIRMED"
    SERVER_UNKNOWN = "SERVER_UNKNOWN"


class ReasoningBackend(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities:
        ...

    def start_session(self, project_label: str) -> str:
        """Return an opaque provider-scoped ID; persistence stays local."""
        ...

    def send_message(self, messages, tools=None, metadata=None) -> None:
        ...

    def stream_response(self) -> BackendReply:
        """Return a stable final reply, not an unverified transport chunk."""
        ...

    def cancel(self) -> CancellationStatus:
        """Cancel local waiting; server termination needs separate proof."""
        ...

    def health_check(self) -> bool:
        ...

    def close(self) -> None:
        ...
