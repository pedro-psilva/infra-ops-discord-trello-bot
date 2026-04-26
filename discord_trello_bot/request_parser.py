from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

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
    r"<@!?\d+>|<@&\d+>|(?:\b(?:cri(?:e|ar|a)|ger(?:e|ar|a)|faca|adicion(?:e|ar|a)|abra)\b.*\bcard\b)",
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

MAX_CONTEXT_MESSAGES = 50
MAX_DETAIL_SENTENCES = 2
SHORT_GAP_MINUTES = 20
LONG_GAP_MINUTES = 90
MIN_ANALYSIS_WINDOW = timedelta(hours=1)


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
        summary = _build_request_summary(
            reply_chain_messages=reply_chain_messages,
            context_messages=context_messages,
        )
        title = _build_card_title(
            instruction=instruction,
            due_fragment=due_fragment,
            summary=summary,
            reply_chain_messages=reply_chain_messages,
            context_messages=context_messages,
        )
        context_excerpt = _build_supporting_details_summary(
            context_messages=context_messages,
            summary=summary,
        )
        source_excerpt = summary

        return (
            RequestedCard(
                title=title,
                summary=summary,
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
    text = re.sub(r"<@&\d+>", " ", text)
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
    summary: str,
    reply_chain_messages: list[DiscordMessage],
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

    if candidate and not _is_generic_request_candidate(candidate):
        normalized = _compact_text(candidate, limit=80)
        if normalized:
            return _normalize_title(normalized)

    summary_title = _title_from_summary(summary)
    if summary_title:
        return summary_title

    source_summary = _pick_source_summary(
        reply_chain_messages=reply_chain_messages,
        context_messages=context_messages,
    )
    return _normalize_title(source_summary)


def _pick_source_summary(
    *,
    reply_chain_messages: list[DiscordMessage],
    context_messages: list[DiscordMessage],
) -> str:
    best_summary = ""
    best_score = -1
    for message in reply_chain_messages + context_messages:
        for sentence in _extract_sentences(message.content):
            summary = _compact_text(sentence, limit=100)
            if not summary:
                continue
            score = _score_summary_sentence(
                summary,
                priority_boost=6 if message in reply_chain_messages else 0,
            )
            if score > best_score:
                best_score = score
                best_summary = summary
    if best_summary:
        return best_summary
    return "Solicitacao recebida no Discord"


def _compact_text(text: str, *, limit: int) -> str:
    text = re.sub(r"<@!?\d+>|<@&\d+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip(" -*\t") for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    compact = re.sub(r"\s{2,}", " ", lines[0]).strip(" ,:-")
    if len(compact) > limit:
        compact = compact[: limit - 3].rstrip() + "..."
    return compact


def _build_supporting_details_summary(
    *,
    context_messages: list[DiscordMessage],
    summary: str,
) -> str:
    summary_terms = _topic_terms(summary)
    summary_keys = {
        _strip_accents(_compact_text(sentence, limit=220).lower()).strip(" .")
        for sentence in _extract_sentences(summary)
        if _compact_text(sentence, limit=220)
    }
    scored_sentences: list[tuple[int, int, int, str]] = []
    seen_keys: set[str] = set()

    for message_index, message in enumerate(context_messages):
        for sentence_index, sentence in enumerate(_extract_sentences(message.content)):
            compact = _compact_text(sentence, limit=220)
            if not compact:
                continue
            normalized_key = _strip_accents(compact.lower()).strip(" .")
            if normalized_key in summary_keys or normalized_key in seen_keys:
                continue
            summary_style_sentence = _normalize_summary_sentence(compact)
            if summary_style_sentence:
                summary_style_key = _strip_accents(summary_style_sentence.lower()).strip(" .")
                if summary_style_key in summary_keys:
                    continue
            seen_keys.add(normalized_key)
            normalized_sentence = _normalize_detail_sentence(compact)
            if not normalized_sentence:
                continue
            normalized_sentence_key = _strip_accents(normalized_sentence.lower()).strip(" .")
            if normalized_sentence_key in summary_keys:
                continue
            candidate_terms = _topic_terms(normalized_sentence)
            if candidate_terms and len(candidate_terms & summary_terms) >= max(2, len(candidate_terms) - 2):
                continue
            overlap = len(_topic_terms(compact) & summary_terms)
            score = overlap * 3
            if _looks_actionable_sentence(compact):
                score += 3
            if _looks_follow_up_sentence(compact):
                score += 2
            if not _is_low_signal(compact):
                score += 1
            if message_index == 0:
                score += 2
            if sentence_index == 0:
                score += 1
            if compact.endswith("?"):
                score -= 5
            if len(compact) <= 180:
                score += 1
            if score <= 1:
                continue
            scored_sentences.append((score, message_index, sentence_index, normalized_sentence))

    if not scored_sentences:
        return ""

    selected_sentences = sorted(
        scored_sentences,
        key=lambda item: (-item[0], item[1], item[2]),
    )[:MAX_DETAIL_SENTENCES]
    ordered_sentences = [
        sentence
        for _, _, _, sentence in sorted(selected_sentences, key=lambda item: (item[1], item[2]))
    ]
    joined = " ".join(ordered_sentences)
    if len(joined) > 600:
        joined = joined[:597].rstrip() + "..."
    return joined


def _compact_multiline(text: str, *, limit: int) -> str:
    text = re.sub(r"<@!?\d+>|<@&\d+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    compact = re.sub(r"\s{2,}", " ", compact).strip(" ,")
    if len(compact) > limit:
        compact = compact[: limit - 3].rstrip() + "..."
    return compact


def _build_request_summary(
    *,
    reply_chain_messages: list[DiscordMessage],
    context_messages: list[DiscordMessage],
) -> str:
    primary_sentence = _pick_primary_summary_sentence(
        reply_chain_messages=reply_chain_messages,
        context_messages=context_messages,
    )
    if not primary_sentence:
        return _pick_source_summary(
            reply_chain_messages=reply_chain_messages,
            context_messages=context_messages,
        )

    summary_parts = [_normalize_summary_sentence(primary_sentence)]
    follow_up_sentence = _pick_follow_up_sentence(
        primary_sentence=primary_sentence,
        reply_chain_messages=reply_chain_messages,
        context_messages=context_messages,
    )
    if follow_up_sentence:
        normalized_follow_up = _normalize_summary_sentence(follow_up_sentence)
        if normalized_follow_up and normalized_follow_up not in summary_parts:
            summary_parts.append(normalized_follow_up)

    return " ".join(part for part in summary_parts if part).strip()


def _pick_primary_summary_sentence(
    *,
    reply_chain_messages: list[DiscordMessage],
    context_messages: list[DiscordMessage],
) -> str:
    best_sentence = ""
    best_score = -1
    seen: set[str] = set()

    for message in reply_chain_messages + context_messages:
        priority_boost = 8 if message in reply_chain_messages else 0
        for sentence_index, sentence in enumerate(_extract_sentences(message.content)):
            normalized_key = _strip_accents(sentence.lower())
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            score = _score_summary_sentence(sentence, priority_boost=priority_boost)
            if _looks_follow_up_sentence(sentence):
                score -= 6
            if message in reply_chain_messages:
                score -= sentence_index * 2
            if score > best_score:
                best_score = score
                best_sentence = sentence

    return best_sentence


def _pick_follow_up_sentence(
    *,
    primary_sentence: str,
    reply_chain_messages: list[DiscordMessage],
    context_messages: list[DiscordMessage],
) -> str:
    primary_terms = _topic_terms(primary_sentence)
    best_sentence = ""
    best_score = -1

    for message in reply_chain_messages + context_messages:
        priority_boost = 5 if message in reply_chain_messages else 0
        for sentence in _extract_sentences(message.content):
            if sentence == primary_sentence:
                continue
            overlap = len(_topic_terms(sentence) & primary_terms)
            if not _looks_follow_up_sentence(sentence):
                continue
            score = _score_summary_sentence(sentence, priority_boost=priority_boost)
            score += overlap * 2
            score += 4
            if score > best_score:
                best_score = score
                best_sentence = sentence

    return best_sentence


def _score_summary_sentence(sentence: str, *, priority_boost: int) -> int:
    compact = _compact_text(sentence, limit=180)
    if not compact:
        return -100

    score = priority_boost + len(_topic_terms(compact))
    if _looks_actionable_sentence(compact):
        score += 8
    if _looks_follow_up_sentence(compact):
        score += 3
    if _is_low_signal(compact):
        score -= 8
    if 20 <= len(compact) <= 160:
        score += 3
    if compact.endswith("?"):
        score -= 3
    if compact.lower().startswith(("mas,", "mas ", "e ", "aí ", "ai ")):
        score -= 1
    return score


def _extract_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"<@!?\d+>|<@&\d+>", "", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    sentences: list[str] = []
    for chunk in chunks:
        sentence = chunk.strip(" -*\t")
        sentence = re.sub(r"\s{2,}", " ", sentence).strip(" ,")
        if sentence:
            sentences.append(sentence)
    return sentences


def _normalize_summary_sentence(sentence: str) -> str:
    sentence = _compact_multiline(sentence, limit=220).strip()
    sentence = re.sub(r"^\s*@[\w.-]+(?:\s+[\w.-]+){0,2}\s*", "", sentence)
    sentence = re.sub(r"^\s*(?:mas,\s*)?", "", sentence, flags=re.IGNORECASE)

    replacements: tuple[tuple[str, str], ...] = (
        (
            r"^faz\s+uma\s+proposta\s+(?:pra\s+mim\s+)?(?:do|da|de)\s+(.+?)\s+com\s+isso\s+que\s+vou\s+validar\s+com\s+(.+)$",
            r"Preparar uma proposta de \1 para validar com \2",
        ),
        (
            r"^faca\s+uma\s+proposta\s+(?:pra\s+mim\s+)?(?:do|da|de)\s+(.+?)\s+com\s+isso\s+que\s+vou\s+validar\s+com\s+(.+)$",
            r"Preparar uma proposta de \1 para validar com \2",
        ),
        (
            r"^faz\s+uma\s+proposta\s+(?:pra\s+mim\s+)?(?:do|da|de)\s+(.+)$",
            r"Preparar uma proposta de \1",
        ),
        (
            r"^faca\s+uma\s+proposta\s+(?:pra\s+mim\s+)?(?:do|da|de)\s+(.+)$",
            r"Preparar uma proposta de \1",
        ),
        (
            r"^ele\s+aprovando,\s+a\s+gente\s+ajusta\s+(.+)$",
            r"Se aprovado, ajustar \1",
        ),
    )

    normalized_ascii = _strip_accents(sentence.lower())
    for pattern, replacement in replacements:
        if re.match(pattern, normalized_ascii, flags=re.IGNORECASE):
            sentence = re.sub(
                pattern,
                replacement,
                _strip_accents(sentence),
                flags=re.IGNORECASE,
            )
            break

    sentence = sentence.strip(" .")
    if not sentence:
        return ""
    sentence = sentence[0].upper() + sentence[1:]
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def _normalize_detail_sentence(sentence: str) -> str:
    sentence = _compact_multiline(sentence, limit=220).strip()
    sentence = re.sub(r"^\s*@[\w.-]+(?:\s+[\w.-]+){0,2}\s*", "", sentence)
    sentence = re.sub(r"^\s*(?:mas|e|ai),\s*", "", sentence, flags=re.IGNORECASE)
    sentence = re.sub(r"^\s*vamos fazer isso,\s*", "", sentence, flags=re.IGNORECASE)

    replacements: tuple[tuple[str, str], ...] = (
        (
            r"^podemos colocar no (.+), nao no (.+)$",
            r"Foi considerado usar \1 em vez de \2",
        ),
        (
            r"^no (.+) eu acho que faz mais sentido(?: \(.+?\))?, por que e certeza que a pessoa vai assinar$",
            r"Tambem foi considerada a alternativa de manter isso em \1, para garantir a assinatura",
        ),
        (
            r"^se nao (.+)$",
            r"Existe urgencia: se nao \1",
        ),
    )

    normalized_ascii = _strip_accents(sentence.lower())
    for pattern, replacement in replacements:
        if re.match(pattern, normalized_ascii, flags=re.IGNORECASE):
            sentence = re.sub(
                pattern,
                replacement,
                _strip_accents(sentence),
                flags=re.IGNORECASE,
            )
            break

    sentence = sentence.strip(" .")
    if not sentence:
        return ""
    sentence = sentence[0].upper() + sentence[1:]
    if sentence.endswith("?"):
        sentence = sentence[:-1].rstrip()
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def _title_from_summary(summary: str) -> str:
    sentences = _extract_sentences(summary)
    if not sentences:
        return ""

    title = sentences[0].strip(" .")
    substitutions: tuple[tuple[str, str], ...] = (
        (r"^Preparar uma proposta de termo\b", "Proposta do termo"),
        (r"^Preparar uma proposta de\s+", "Proposta de "),
        (r"^Preparar proposta de\s+", "Proposta de "),
        (r"^Preparar uma proposta do\s+", "Proposta do "),
        (r"^Preparar proposta do\s+", "Proposta do "),
        (r"^Precisamos revisar o\s+", "Revisar "),
        (r"^Precisamos revisar\s+", "Revisar "),
        (r"^Revisar o\s+", "Revisar "),
        (r"^Ajustar os\s+", "Ajustar "),
    )
    for pattern, replacement in substitutions:
        title = re.sub(pattern, replacement, title, flags=re.IGNORECASE)

    title = re.sub(r"\s+", " ", title).strip(" ,:-")
    if len(title) > 90:
        title = title[:87].rstrip() + "..."
    return _normalize_title(title)


def _normalize_title(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip(" ,:-")
    return normalized


def _is_generic_request_candidate(candidate: str) -> bool:
    normalized = _strip_accents(candidate.lower())
    generic_fragments = (
        "sobre isso",
        "sobre essa mensagem",
        "sobre esta mensagem",
        "disso",
        "pra ",
        "para ",
    )
    return any(fragment in normalized for fragment in generic_fragments)


def _looks_actionable_sentence(text: str) -> bool:
    normalized = _strip_accents(text.lower())
    actionable_patterns = (
        r"\bprecisamos\b",
        r"\bfaz(?:er)?\b",
        r"\bfaca\b",
        r"\bproposta\b",
        r"\bajust",
        r"\bvalid",
        r"\brevis",
        r"\bmigr",
        r"\bcriar\b",
        r"\babrir\b",
        r"\btermo\b",
        r"\bcontrato\b",
        r"\bprocess",
        r"\bdesligamento\b",
    )
    return any(re.search(pattern, normalized) for pattern in actionable_patterns)


def _looks_follow_up_sentence(text: str) -> bool:
    normalized = _strip_accents(text.lower())
    return any(
        fragment in normalized
        for fragment in (
            "se aprovado",
            "ele aprovando",
            "depois",
            "ajusta os outros processos",
            "ajustar os outros processos",
        )
    )


def _select_context_messages(
    *,
    command_message: DiscordMessage,
    recent_channel_messages: list[DiscordMessage],
    reply_chain_messages: list[DiscordMessage],
    timezone,
) -> list[DiscordMessage]:
    minimum_window_start = command_message.timestamp.astimezone(timezone) - MIN_ANALYSIS_WINDOW
    recent_window_messages = [
        message
        for message in recent_channel_messages
        if message.id != command_message.id
        and message.timestamp < command_message.timestamp
        and _is_context_candidate(message)
    ]
    ordered_messages = _merge_messages(recent_window_messages, reply_chain_messages)
    if not ordered_messages:
        return []

    if reply_chain_messages:
        selected_messages = [message for message in reply_chain_messages if _is_context_candidate(message)]
    else:
        last_hour_messages = [
            message
            for message in ordered_messages
            if message.timestamp.astimezone(timezone) >= minimum_window_start
        ]
        selected_messages = [last_hour_messages[-1] if last_hour_messages else ordered_messages[-1]]
    if not selected_messages:
        return []

    selected_ids = {message.id for message in selected_messages}
    participants = {message.author_id for message in selected_messages}
    topic_terms = set().union(*(_topic_terms(message.content) for message in selected_messages))
    seed_terms = set(topic_terms)
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
            seed_terms=seed_terms,
            allow_loose_bridge=bool(reply_chain_messages),
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
            seed_terms=seed_terms,
            allow_loose_bridge=bool(reply_chain_messages),
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
    seed_terms: set[str],
    allow_loose_bridge: bool,
) -> bool:
    if not _is_context_candidate(candidate):
        return False
    gap_minutes = abs((adjacent_message.timestamp - candidate.timestamp).total_seconds()) / 60
    if gap_minutes > LONG_GAP_MINUTES:
        return False
    if _looks_like_bot_command(candidate.content):
        return False

    candidate_terms = _topic_terms(candidate.content)
    adjacent_terms = _topic_terms(adjacent_message.content)
    overlap = len(candidate_terms & topic_terms)
    seed_overlap = len(candidate_terms & seed_terms)
    adjacent_overlap = len(candidate_terms & adjacent_terms)
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
    if seed_overlap >= 1:
        score += 2
    if seed_overlap >= 2:
        score += 1
    if adjacent_overlap >= 1:
        score += 2
    if adjacent_overlap >= 2:
        score += 1
    if gap_minutes <= SHORT_GAP_MINUTES:
        score += 1
        if adjacent_message.author_id in participants:
            score += 1
    if candidate.author_id == adjacent_message.author_id:
        score += 1
    if _looks_actionable_sentence(candidate.content):
        score += 1
    if _is_low_signal(candidate.content):
        score -= 1

    if score >= 3:
        return True
    return (
        allow_loose_bridge
        and len(selected_ids) == 1
        and gap_minutes <= SHORT_GAP_MINUTES
        and not _is_low_signal(candidate.content)
    )


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
    compact = re.sub(r"<@!?\d+>|<@&\d+>", " ", compact)
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
