from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class TaskType(StrEnum):
    ONBOARDING = "onboarding"
    OFFBOARDING = "offboarding"

    @property
    def label_pt_br(self) -> str:
        return "Onboarding" if self is TaskType.ONBOARDING else "Offboarding"


@dataclass(frozen=True)
class DiscordReaction:
    emoji_name: str | None
    emoji_id: str | None
    me: bool

    @classmethod
    def from_api(cls, payload: dict) -> "DiscordReaction":
        emoji = payload.get("emoji") or {}
        return cls(
            emoji_name=emoji.get("name"),
            emoji_id=emoji.get("id"),
            me=bool(payload.get("me")),
        )


@dataclass(frozen=True)
class DiscordMessage:
    id: str
    channel_id: str
    content: str
    timestamp: datetime
    author_id: str
    author_name: str
    author_is_bot: bool
    message_type: int
    webhook_id: str | None
    referenced_message_id: str | None
    reactions: tuple[DiscordReaction, ...]

    @classmethod
    def from_api(cls, payload: dict) -> "DiscordMessage":
        author = payload.get("author") or {}
        timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        reactions = tuple(DiscordReaction.from_api(item) for item in payload.get("reactions", []))
        message_reference = payload.get("message_reference") or {}
        return cls(
            id=str(payload["id"]),
            channel_id=str(payload["channel_id"]),
            content=(payload.get("content") or "").strip(),
            timestamp=timestamp,
            author_id=str(author.get("id") or ""),
            author_name=str(author.get("global_name") or author.get("username") or "desconhecido"),
            author_is_bot=bool(author.get("bot")),
            message_type=int(payload.get("type", 0)),
            webhook_id=payload.get("webhook_id"),
            referenced_message_id=(
                str(message_reference.get("message_id"))
                if message_reference.get("message_id") is not None
                else None
            ),
            reactions=reactions,
        )

    def has_confirmation_reaction(self, emoji_name: str) -> bool:
        return any(reaction.me and reaction.emoji_name == emoji_name for reaction in self.reactions)


@dataclass(frozen=True)
class ParsedTask:
    task_type: TaskType
    employee_name: str
    effective_date: date
    notes: tuple[str, ...]
    raw_excerpt: str


@dataclass(frozen=True)
class ParseResult:
    tasks: tuple[ParsedTask, ...] = ()
    reason: str | None = None

    @property
    def task(self) -> ParsedTask | None:
        return self.tasks[0] if self.tasks else None


@dataclass
class RunSummary:
    channels_scanned: int = 0
    messages_scanned: int = 0
    tasks_parsed: int = 0
    cards_created: int = 0
    messages_already_confirmed: int = 0
    messages_skipped: int = 0
    errors: int = 0
