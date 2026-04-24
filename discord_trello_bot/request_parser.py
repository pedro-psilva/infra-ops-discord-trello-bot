from __future__ import annotations

import re
from datetime import date, datetime

import dateparser
from dateparser.search import search_dates

from .config import Settings
from .models import DiscordMessage, RequestedCard


GENERIC_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcri(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\bger(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\bfa[çc]a\b", re.IGNORECASE),
    re.compile(r"\badicion(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\babra\b", re.IGNORECASE),
)

CARD_KEYWORD_PATTERN = re.compile(r"\bcard\b", re.IGNORECASE)

GENERIC_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:sobre\s+isso|sobre\s+essa\s+mensagem|sobre\s+esta\s+mensagem|disso)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:pra|para|at[eé])\s*$", re.IGNORECASE),
)


class RequestParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse(
        self,
        *,
        command_message: DiscordMessage,
        bot_user_id: str,
        context_messages: list[DiscordMessage],
    ) -> tuple[RequestedCard | None, str | None]:
        instruction = _normalize_instruction(command_message.content, bot_user_id)
        if not instruction:
            return None, "pedido sem instrucao apos a mencao"
        if not _looks_like_card_request(instruction):
            return None, "mencao sem pedido de card"
        if not context_messages:
            return None, "mensagem de contexto nao encontrada"

        due_date, due_fragment = _extract_due_date(
            text=instruction,
            relative_base=command_message.timestamp.astimezone(self.settings.timezone),
            timezone_name=str(self.settings.timezone),
        )
        title = _build_card_title(
            instruction=instruction,
            due_fragment=due_fragment,
            context_messages=context_messages,
        )
        context_excerpt = _build_context_excerpt(context_messages)
        source_excerpt = context_messages[-1].content[:1800]

        return (
            RequestedCard(
                title=title,
                due_date=due_date,
                instruction=instruction,
                source_excerpt=source_excerpt,
                context_excerpt=context_excerpt,
            ),
            None,
        )


def _normalize_instruction(text: str, bot_user_id: str) -> str:
    text = text.replace(f"<@{bot_user_id}>", " ").replace(f"<@!{bot_user_id}>", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip(" ,:-")


def _looks_like_card_request(instruction: str) -> bool:
    return CARD_KEYWORD_PATTERN.search(instruction) is not None and any(
        pattern.search(instruction) for pattern in GENERIC_COMMAND_PATTERNS
    )


def _extract_due_date(text: str, relative_base: datetime, timezone_name: str) -> tuple[date | None, str | None]:
    settings = {
        "RELATIVE_BASE": relative_base,
        "DATE_ORDER": "DMY",
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DATES_FROM": "future",
    }
    matches = search_dates(text, languages=["pt", "en"], settings=settings) or []
    for fragment, parsed in matches:
        if not _looks_like_due_fragment(fragment):
            continue
        return parsed.date(), fragment
    return None, None


def _looks_like_due_fragment(fragment: str) -> bool:
    if re.search(r"\d", fragment):
        return True

    lowered = fragment.lower()
    weekday_keywords = (
        "segunda",
        "terca",
        "terça",
        "quarta",
        "quinta",
        "sexta",
        "sabado",
        "sábado",
        "domingo",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    return any(keyword in lowered for keyword in weekday_keywords)


def _build_card_title(
    *,
    instruction: str,
    due_fragment: str | None,
    context_messages: list[DiscordMessage],
) -> str:
    candidate = instruction
    candidate = re.sub(
        r"^\s*(?:por favor[, ]*)?(?:cri(?:e|ar|a)|ger(?:e|ar|a)|fa[çc]a|adicion(?:e|ar|a)|abra)\s+(?:um\s+)?card\b",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    if due_fragment:
        candidate = re.sub(
            rf"(?:\b(?:pra|para|at[eé])\b\s*)?{re.escape(due_fragment)}",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    candidate = re.sub(
        r"\b(?:sobre\s+isso|sobre\s+essa\s+mensagem|sobre\s+esta\s+mensagem|disso)\b",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"\bfeira\b", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip(" ,:-")
    if any(pattern.match(candidate) for pattern in GENERIC_TITLE_PATTERNS):
        candidate = ""

    if candidate:
        normalized = _compact_text(candidate, limit=80)
        if normalized:
            return f"[Discord] {normalized}"

    source_summary = _pick_source_summary(context_messages)
    return f"[Discord] {source_summary}"


def _pick_source_summary(context_messages: list[DiscordMessage]) -> str:
    for message in reversed(context_messages):
        summary = _compact_text(message.content, limit=80)
        if summary:
            return summary
    return "Solicitacao recebida no Discord"


def _compact_text(text: str, *, limit: int) -> str:
    text = re.sub(r"<@!?\d+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip(" -*\t") for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    compact = re.sub(r"\s{2,}", " ", lines[0]).strip(" ,:-")
    if len(compact) > limit:
        compact = compact[: limit - 3].rstrip() + "..."
    return compact


def _build_context_excerpt(context_messages: list[DiscordMessage]) -> str:
    lines: list[str] = []
    for message in context_messages:
        body = _compact_multiline(message.content, limit=800)
        if not body:
            continue
        lines.append(f"{message.author_name}: {body}")

    joined = "\n".join(lines).strip()
    if len(joined) > 1800:
        joined = joined[:1797].rstrip() + "..."
    return joined


def _compact_multiline(text: str, *, limit: int) -> str:
    text = re.sub(r"<@!?\d+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    compact = re.sub(r"\s{2,}", " ", compact).strip(" ,")
    if len(compact) > limit:
        compact = compact[: limit - 3].rstrip() + "..."
    return compact
