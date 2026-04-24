from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from dateparser.search import search_dates

from .config import Settings
from .models import DiscordMessage, RequestedCard


GENERIC_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcri(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\bger(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\bfaca\b", re.IGNORECASE),
    re.compile(r"\badicion(?:e|ar|a)\b", re.IGNORECASE),
    re.compile(r"\babra\b", re.IGNORECASE),
)

CARD_KEYWORD_PATTERN = re.compile(r"\bcard\b", re.IGNORECASE)
BOT_COMMAND_PATTERN = re.compile(
    r"<@!?\d+>|(?:\b(?:cri(?:e|ar|a)|ger(?:e|ar|a)|faca|adicion(?:e|ar|a)|abra)\b.*\bcard\b)",
    re.IGNORECASE,
)

GENERIC_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:sobre\s+isso|sobre\s+essa\s+mensagem|sobre\s+esta\s+mensagem|disso)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:pra|para|ate)\s*$", re.IGNORECASE),
)

LOW_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:ok|okay|blz|beleza|show|fechado|perfeito|valeu|obrigad[oa]|bom\s+dia|boa\s+tarde|boa\s+noite)[!. ]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:kk+|rs+|:+\)+|:+\(+)$", re.IGNORECASE),
)

TOPIC_STOPWORDS = {
    "a",
    "agora",
    "ai",
    "ainda",
    "algo",
    "algum",
    "alguma",
    "ao",
    "aos",
    "as",
    "ate",
    "bot",
    "card",
    "com",
    "como",
    "da",
    "das",
    "de",
    "dele",
    "dela",
    "do",
    "dos",
    "e",
    "ela",
    "ele",
    "em",
    "essa",
    "esse",
    "esta",
    "este",
    "eu",
    "foi",
    "hoje",
    "isso",
    "isto",
    "ja",
    "mais",
    "mas",
    "me",
    "mesmo",
    "minha",
    "meu",
    "na",
    "nas",
    "no",
    "nos",
    "o",
    "os",
    "ou",
    "para",
    "por",
    "pra",
    "que",
    "sem",
    "ser",
    "sobre",
    "so",
    "ta",
    "tem",
    "uma",
    "um",
    "vai",
}

MAX_CONTEXT_MESSAGES = 12
SHORT_GAP_MINUTES = 20
LONG_GAP_MINUTES = 90


class RequestParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse(
        self,
        *,
        command_message: DiscordMessage,
        bot_user_id: str,
        recent_channel_messages: list[DiscordMessage],
        reply_chain_messages: list[DiscordMessage],
    ) -> tuple[RequestedCard | None, list[DiscordMessage], str | None]:
        instruction = _normalize_instruction(command_message.content, bot_user_id)
        if not instruction:
            return None, [], "pedido sem instrucao apos a mencao"
        if not _looks_like_card_request(instruction):
            return None, [], "mencao sem pedido de card"

        context_messages = _select_context_messages(
            command_message=command_message,
            recent_channel_messages=recent_channel_messages,
            reply_chain_messages=reply_chain_messages,
            timezone=self.settings.timezone,
        )
        if not context_messages:
            return None, [], "mensagem de contexto nao encontrada"

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
        source_excerpt = _pick_source_summary(context_messages)

        return (
            RequestedCard(
                title=title,
                due_date=due_date,
                instruction=instruction,
                source_excerpt=source_excerpt,
                context_excerpt=context_excerpt,
            ),
            context_messages,
            None,
        )


def _normalize_instruction(text: str, bot_user_id: str) -> str:
    text = text.replace(f"<@{bot_user_id}>", " ").replace(f"<@!{bot_user_id}>", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip(" ,:-")


def _looks_like_card_request(instruction: str) -> bool:
    normalized = _strip_accents(instruction.lower())
    return CARD_KEYWORD_PATTERN.search(normalized) is not None and any(
        pattern.search(normalized) for pattern in GENERIC_COMMAND_PATTERNS
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
    normalized = _strip_accents(fragment.lower())
    if re.search(r"\d", normalized):
        return True

    weekday_keywords = (
        "segunda",
        "terca",
        "quarta",
        "quinta",
        "sexta",
        "sabado",
        "domingo",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    return any(keyword in normalized for keyword in weekday_keywords)


def _build_card_title(
    *,
    instruction: str,
    due_fragment: str | None,
    context_messages: list[DiscordMessage],
) -> str:
    candidate = _strip_accents(instruction)
    candidate = re.sub(
        r"^\s*(?:por favor[, ]*)?(?:cri(?:e|ar|a)|ger(?:e|ar|a)|faca|adicion(?:e|ar|a)|abra)\s+(?:um\s+)?card\b",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    if due_fragment:
        candidate = re.sub(
            rf"(?:\b(?:pra|para|ate)\b\s*)?{re.escape(_strip_accents(due_fragment))}",
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
    best_summary = ""
    best_score = -1
    for message in context_messages:
        summary = _compact_text(message.content, limit=80)
        if not summary:
            continue
        score = len(_topic_terms(summary))
        if not _is_low_signal(summary):
            score += 4
        if 24 <= len(summary) <= 120:
            score += 2
        if message.referenced_message_id is None:
            score += 1
        if score > best_score:
            best_score = score
            best_summary = summary
    if best_summary:
        return best_summary
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


def _select_context_messages(
    *,
    command_message: DiscordMessage,
    recent_channel_messages: list[DiscordMessage],
    reply_chain_messages: list[DiscordMessage],
    timezone,
) -> list[DiscordMessage]:
    day_start = command_message.timestamp.astimezone(timezone).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    recent_same_day_messages = [
        message
        for message in recent_channel_messages
        if message.id != command_message.id
        and message.timestamp < command_message.timestamp
        and message.timestamp.astimezone(timezone) >= day_start
        and _is_context_candidate(message)
    ]
    ordered_messages = _merge_messages(recent_same_day_messages, reply_chain_messages)
    if not ordered_messages:
        return []

    if reply_chain_messages:
        selected_messages = [message for message in reply_chain_messages if _is_context_candidate(message)]
    else:
        selected_messages = [ordered_messages[-1]]
    if not selected_messages:
        return []

    selected_ids = {message.id for message in selected_messages}
    participants = {message.author_id for message in selected_messages}
    topic_terms = set().union(*(_topic_terms(message.content) for message in selected_messages))
    index_by_id = {message.id: index for index, message in enumerate(ordered_messages)}
    left_index = min(index_by_id[message.id] for message in selected_messages if message.id in index_by_id)
    right_index = max(index_by_id[message.id] for message in selected_messages if message.id in index_by_id)

    while left_index > 0 and len(selected_messages) < MAX_CONTEXT_MESSAGES:
        candidate = ordered_messages[left_index - 1]
        adjacent_message = ordered_messages[left_index]
        if not _should_extend_segment(
            candidate=candidate,
            adjacent_message=adjacent_message,
            selected_ids=selected_ids,
            participants=participants,
            topic_terms=topic_terms,
        ):
            break
        left_index -= 1
        selected_messages.insert(0, candidate)
        selected_ids.add(candidate.id)
        participants.add(candidate.author_id)
        topic_terms.update(_topic_terms(candidate.content))

    while right_index + 1 < len(ordered_messages) and len(selected_messages) < MAX_CONTEXT_MESSAGES:
        candidate = ordered_messages[right_index + 1]
        adjacent_message = ordered_messages[right_index]
        if not _should_extend_segment(
            candidate=candidate,
            adjacent_message=adjacent_message,
            selected_ids=selected_ids,
            participants=participants,
            topic_terms=topic_terms,
        ):
            break
        right_index += 1
        selected_messages.append(candidate)
        selected_ids.add(candidate.id)
        participants.add(candidate.author_id)
        topic_terms.update(_topic_terms(candidate.content))

    return selected_messages


def _merge_messages(*groups: list[DiscordMessage]) -> list[DiscordMessage]:
    deduped: dict[str, DiscordMessage] = {}
    for group in groups:
        for message in group:
            deduped[message.id] = message
    return sorted(
        deduped.values(),
        key=lambda message: (message.timestamp, message.id),
    )


def _is_context_candidate(message: DiscordMessage) -> bool:
    if message.author_is_bot or message.webhook_id:
        return False
    if message.message_type not in {0, 19}:
        return False
    return bool(message.content.strip())


def _should_extend_segment(
    *,
    candidate: DiscordMessage,
    adjacent_message: DiscordMessage,
    selected_ids: set[str],
    participants: set[str],
    topic_terms: set[str],
) -> bool:
    if not _is_context_candidate(candidate):
        return False
    gap_minutes = abs((adjacent_message.timestamp - candidate.timestamp).total_seconds()) / 60
    if gap_minutes > LONG_GAP_MINUTES:
        return False
    if _looks_like_bot_command(candidate.content):
        return False

    overlap = len(_topic_terms(candidate.content) & topic_terms)
    score = 0
    if candidate.referenced_message_id and candidate.referenced_message_id in selected_ids:
        score += 4
    if adjacent_message.referenced_message_id == candidate.id:
        score += 4
    if candidate.author_id in participants:
        score += 1
    if overlap >= 1:
        score += 2
    if overlap >= 2:
        score += 1
    if gap_minutes <= SHORT_GAP_MINUTES:
        score += 1
        if adjacent_message.author_id in participants:
            score += 1
    if _is_low_signal(candidate.content):
        score -= 1

    return score >= 2


def _looks_like_bot_command(text: str) -> bool:
    compact = _compact_multiline(_strip_accents(text), limit=200)
    if not compact:
        return False
    return BOT_COMMAND_PATTERN.search(compact) is not None


def _is_low_signal(text: str) -> bool:
    compact = _compact_multiline(_strip_accents(text), limit=120)
    if not compact:
        return True
    return any(pattern.match(compact) for pattern in LOW_SIGNAL_PATTERNS)


def _topic_terms(text: str) -> set[str]:
    compact = _compact_multiline(text, limit=400)
    compact = re.sub(r"<@!?\d+>", " ", compact)
    compact = re.sub(r"https?://\S+", " ", compact)
    compact = _strip_accents(compact.lower())
    terms = set(re.findall(r"[a-z0-9]{3,}", compact))
    return {
        term
        for term in terms
        if term not in TOPIC_STOPWORDS and not term.isdigit()
    }


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(character for character in normalized if unicodedata.category(character) != "Mn")
