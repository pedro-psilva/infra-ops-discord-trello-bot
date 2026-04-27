from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone as _tz

UTC = _tz.utc

from .config import ConfirmationMode, Settings
from .discord_api import DiscordApiClient, build_discord_message_url
from .gmail_api import GmailApiClient
from .http import ApiError
from .models import DiscordMessage, EmailMessage, ParsedTask, RequestedCard, RunSummary, TaskCard, TaskType
from .openai_request_refiner import OpenAIRequestRefiner
from .parser import NOTE_KEYWORDS, TaskParser
from .request_parser import RequestParser
from .trello_api import TrelloApiClient


LOGGER = logging.getLogger(__name__)

SUPPORTED_MESSAGE_TYPES = {0, 19}


class DiscordTrelloService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discord = DiscordApiClient(settings)
        self.trello = TrelloApiClient(settings)
        self.parser = TaskParser(settings)
        self.request_parser = RequestParser(settings)
        self.request_refiner = (
            OpenAIRequestRefiner(settings)
            if settings.openai_api_key
            else None
        )
        self.gmail = (
            GmailApiClient(settings)
            if settings.gmail_user_email
            else None
        )

    def run(self) -> RunSummary:
        summary = RunSummary()
        cutoff = datetime.now(tz=self.settings.timezone) - timedelta(days=self.settings.lookback_days)
        channel_modes = self._build_channel_modes()
        bot_user_id = self.discord.get_current_user_id() if self.settings.discord_request_channel_ids else None
        bot_role_ids = (
            set(self.discord.get_current_member_role_ids())
            if self.settings.discord_request_channel_ids
            else set()
        )

        for channel_id, modes in channel_modes.items():
            summary.channels_scanned += 1
            LOGGER.info("Processando canal %s desde %s.", channel_id, cutoff.isoformat())

            try:
                messages = self.discord.iter_messages_since(channel_id, cutoff)
            except ApiError:
                summary.errors += 1
                LOGGER.exception("Falha ao listar mensagens do canal %s.", channel_id)
                continue

            bot_reply_reference_ids = {
                message.referenced_message_id
                for message in messages
                if message.author_is_bot and message.referenced_message_id
            }

            for message in messages:
                summary.messages_scanned += 1
                outcome, created_count = self._process_message(
                    message,
                    channel_messages=messages,
                    bot_reply_reference_ids=bot_reply_reference_ids,
                    modes=modes,
                    bot_user_id=bot_user_id,
                    bot_role_ids=bot_role_ids,
                )

                if outcome == "already_confirmed":
                    summary.messages_already_confirmed += 1
                elif outcome == "skipped":
                    summary.messages_skipped += 1
                elif outcome == "created":
                    summary.tasks_parsed += created_count
                    summary.cards_created += created_count
                elif outcome == "error":
                    summary.errors += 1

        self._process_gmail_messages(summary)
        return summary

    def _process_message(
        self,
        message: DiscordMessage,
        *,
        channel_messages: list[DiscordMessage],
        bot_reply_reference_ids: set[str],
        modes: set[str],
        bot_user_id: str | None,
        bot_role_ids: set[str],
    ) -> tuple[str, int]:
        if not self._should_consider_message(message):
            return "skipped", 0

        if message.has_confirmation_reaction(self.settings.discord_reaction_emoji):
            LOGGER.info("Mensagem %s ja possui confirmacao do bot.", message.id)
            return "already_confirmed", 0
        if message.id in bot_reply_reference_ids:
            LOGGER.info("Mensagem %s ja possui resposta de confirmacao do bot.", message.id)
            return "already_confirmed", 0

        if "request" in modes:
            request_outcome = self._process_request_message(
                message,
                channel_messages=channel_messages,
                bot_user_id=bot_user_id,
                bot_role_ids=bot_role_ids,
            )
            if request_outcome is not None:
                return request_outcome

        if "structured" not in modes:
            return "skipped", 0

        parse_result = self.parser.parse_message(message)
        if not parse_result.tasks:
            complement_outcome = self._process_task_complement_message(message)
            if complement_outcome is not None:
                return complement_outcome
            LOGGER.info("Mensagem %s ignorada: %s.", message.id, parse_result.reason)
            return "skipped", 0

        LOGGER.info("Mensagem %s gerou %s tarefa(s).", message.id, len(parse_result.tasks))

        try:
            card_urls: list[str] = []
            created_count = 0
            for task in parse_result.tasks:
                if self._is_past_task(task):
                    LOGGER.info(
                        "Tarefa da mensagem %s ignorada por estar no passado: %s %s.",
                        message.id,
                        task.employee_name,
                        task.effective_date.isoformat(),
                    )
                    continue
                LOGGER.info(
                    "Tarefa detectada na mensagem %s: %s %s em %s.",
                    message.id,
                    task.task_type.value,
                    task.employee_name,
                    task.effective_date.isoformat(),
                )
                card, _was_created = self._get_or_create_task_card(task)
                if _was_created:
                    created_count += 1
                card_id = str(card["id"])
                card_url = str(card["url"])
                card_urls.append(card_url)
                self.trello.add_comment(
                    card_id=card_id,
                    text=self._build_trello_comment(task=task, message=message, card_url=card_url),
                )

            if not card_urls:
                return "skipped", 0

            self.discord.add_reaction(
                channel_id=message.channel_id,
                message_id=message.id,
                emoji=self.settings.discord_reaction_emoji,
            )

            if self.settings.discord_confirmation_mode in {ConfirmationMode.REPLY, ConfirmationMode.BOTH}:
                self.discord.reply_to_message(
                    channel_id=message.channel_id,
                    message_id=message.id,
                    content=self._build_discord_reply(card_urls),
                )

            return "created", created_count
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao processar a mensagem %s.", message.id)
            return "error", 0

    def _process_request_message(
        self,
        message: DiscordMessage,
        *,
        channel_messages: list[DiscordMessage],
        bot_user_id: str | None,
        bot_role_ids: set[str],
    ) -> tuple[str, int] | None:
        if bot_user_id is None:
            return None
        if not message.mentions_user(bot_user_id) and not message.mentions_any_role(bot_role_ids):
            return None

        try:
            recent_channel_messages = self._load_recent_request_messages(
                command_message=message,
                channel_messages=channel_messages,
            )
            context_messages = self._load_reply_chain_messages(message)
        except ApiError:
            LOGGER.exception("Falha ao carregar contexto da mensagem %s.", message.id)
            return "error", 0
        requested_card, selected_context_messages, reason = self.request_parser.parse(
            command_message=message,
            bot_user_id=bot_user_id,
            recent_channel_messages=recent_channel_messages,
            reply_chain_messages=context_messages,
        )
        if requested_card is None:
            LOGGER.info("Mensagem %s ignorada: %s.", message.id, reason)
            return "skipped", 0

        try:
            requested_card = self._refine_requested_card(
                requested_card=requested_card,
                command_message=message,
                selected_context_messages=selected_context_messages,
            )
            card = self.trello.create_card(
                card_name=self._build_requested_card_name(requested_card),
                due_iso=(
                    self._build_due_iso(requested_card.due_date)
                    if requested_card.due_date is not None
                    else None
                ),
                desc=self._build_requested_card_desc(
                    requested_card=requested_card,
                    command_message=message,
                ),
            )
            card_id = str(card["id"])
            card_url = str(card["url"])
            self.trello.add_comment(
                card_id=card_id,
                text=self._build_request_trello_comment(
                    command_message=message,
                    card_url=card_url,
                ),
            )
            self.discord.add_reaction(
                channel_id=message.channel_id,
                message_id=message.id,
                emoji=self.settings.discord_reaction_emoji,
            )

            if self.settings.discord_confirmation_mode in {ConfirmationMode.REPLY, ConfirmationMode.BOTH}:
                self.discord.reply_to_message(
                    channel_id=message.channel_id,
                    message_id=message.id,
                    content=self.settings.discord_reply_template.format(card_url=card_url),
                )

            LOGGER.info(
                "Pedido por mencao processado na mensagem %s. Card criado: %s.",
                message.id,
                card_url,
            )
            return "created", 1
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao processar pedido com mencao na mensagem %s.", message.id)
            return "error", 0

    def _refine_requested_card(
        self,
        *,
        requested_card: RequestedCard,
        command_message: DiscordMessage,
        selected_context_messages: list[DiscordMessage],
    ) -> RequestedCard:
        if self.request_refiner is None:
            return requested_card
        try:
            refined = self.request_refiner.refine(
                requested_card=requested_card,
                command_message=command_message,
                context_messages=selected_context_messages,
            )
        except (ApiError, ValueError):
            LOGGER.exception(
                "Falha ao refinar pedido com OpenAI na mensagem %s. Seguindo com heuristica local.",
                command_message.id,
            )
            return requested_card

        LOGGER.info(
            "Pedido por mencao refinado com OpenAI na mensagem %s. titulo=%s",
            command_message.id,
            refined.title,
        )
        return refined

    def _should_consider_message(self, message: DiscordMessage) -> bool:
        if message.author_is_bot or message.webhook_id:
            return False
        if message.message_type not in SUPPORTED_MESSAGE_TYPES:
            return False
        if not message.content:
            return False
        return True

    def _process_task_complement_message(self, message: DiscordMessage) -> tuple[str, int] | None:
        if not _has_complement_signal(message.content):
            return None

        today = datetime.now(tz=self.settings.timezone).date()
        future_onboarding_cards = [
            card
            for card in self.trello.list_open_task_cards(TaskType.ONBOARDING)
            if card.effective_date >= today
        ]
        matched_cards = _match_complement_cards(
            text=message.content,
            cards=future_onboarding_cards,
        )
        if not matched_cards:
            return None

        try:
            for card in matched_cards:
                self.trello.add_comment(
                    card_id=card.id,
                    text=self._build_complement_trello_comment(
                        card=card,
                        message=message,
                    ),
                )

            self.discord.add_reaction(
                channel_id=message.channel_id,
                message_id=message.id,
                emoji=self.settings.discord_reaction_emoji,
            )
            LOGGER.info(
                "Mensagem %s comentada em %s card(s) futuro(s) de onboarding.",
                message.id,
                len(matched_cards),
            )
            return "created", 0
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao comentar complemento da mensagem %s.", message.id)
            return "error", 0

    def _process_gmail_messages(self, summary: RunSummary) -> None:
        if self.gmail is None:
            return

        LOGGER.info("Processando e-mails do Gmail para %s.", self.settings.gmail_user_email)
        try:
            processed_label_id = self.gmail.processed_label_id()
            messages = self.gmail.list_messages()
        except (ApiError, ValueError):
            summary.errors += 1
            LOGGER.exception("Falha ao listar mensagens do Gmail.")
            return

        for email_message in messages:
            summary.emails_scanned += 1
            if processed_label_id in email_message.label_ids:
                LOGGER.info("E-mail %s ja possui label de processado.", email_message.id)
                continue

            outcome, created_count = self._process_email_message(email_message)
            if outcome == "created":
                summary.tasks_parsed += created_count
                summary.cards_created += created_count
            elif outcome == "skipped":
                summary.messages_skipped += 1
            elif outcome == "error":
                summary.errors += 1

    def _process_email_message(self, email_message: EmailMessage) -> tuple[str, int]:
        synthetic_message = self._build_email_task_message(email_message)
        parse_result = self.parser.parse_message(synthetic_message)
        if not parse_result.tasks:
            LOGGER.info("E-mail %s ignorado: %s.", email_message.id, parse_result.reason)
            return "skipped", 0

        try:
            processed_count = 0
            created_count = 0
            for task in parse_result.tasks:
                if self._is_past_task(task):
                    LOGGER.info(
                        "Tarefa do e-mail %s ignorada por estar no passado: %s %s.",
                        email_message.id,
                        task.employee_name,
                        task.effective_date.isoformat(),
                    )
                    continue
                LOGGER.info(
                    "Tarefa detectada no e-mail %s: %s %s em %s.",
                    email_message.id,
                    task.task_type.value,
                    task.employee_name,
                    task.effective_date.isoformat(),
                )
                card, _was_created = self._get_or_create_task_card(task)
                if _was_created:
                    created_count += 1
                self.trello.add_comment(
                    card_id=str(card["id"]),
                    text=self._build_email_trello_comment(
                        task=task,
                        email_message=email_message,
                        card_url=str(card["url"]),
                    ),
                )
                processed_count += 1

            if processed_count == 0:
                return "skipped", 0

            if self.gmail is not None:
                self.gmail.mark_processed(email_message.id)
            return "created", created_count
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao processar e-mail %s.", email_message.id)
            return "error", 0

    def _get_or_create_task_card(self, task: ParsedTask) -> tuple[dict, bool]:
        card_name = self._build_card_name(task)
        existing_card = self.trello.find_open_card_by_name(card_name)
        if existing_card is not None:
            LOGGER.info("Card existente encontrado no Trello: %s.", card_name)
            return existing_card, False

        card = self.trello.create_card_from_template(
            card_name=card_name,
            due_iso=self._build_due_iso(task.effective_date),
            task_type=task.task_type,
        )
        return card, True

    def _is_past_task(self, task: ParsedTask) -> bool:
        today = datetime.now(tz=self.settings.timezone).date()
        return task.effective_date < today

    def _build_email_task_message(self, email_message: EmailMessage) -> DiscordMessage:
        content = "\n".join(
            part
            for part in (email_message.subject, email_message.body)
            if part.strip()
        )
        return DiscordMessage(
            id=email_message.id,
            channel_id="gmail",
            content=content,
            timestamp=email_message.timestamp,
            author_id=email_message.sender,
            author_name=email_message.sender or self.settings.gmail_user_email or "Gmail",
            author_is_bot=False,
            message_type=0,
            webhook_id=None,
            referenced_channel_id=None,
            referenced_message_id=None,
            mentioned_user_ids=(),
            mentioned_role_ids=(),
            reactions=(),
        )

    def _build_card_name(self, task: ParsedTask) -> str:
        date_label = task.effective_date.strftime("%d/%m/%Y")
        return f"[{task.task_type.label_pt_br}] {task.employee_name} - {date_label}"

    def _build_requested_card_name(self, requested_card: RequestedCard) -> str:
        title = requested_card.title
        if title.startswith("[Discord] "):
            title = title[len("[Discord] "):]
        return title

    def _build_due_iso(self, task_date: date) -> str:
        local_noon = datetime.combine(task_date, time(hour=12), tzinfo=self.settings.timezone)
        due_utc = local_noon.astimezone(UTC).replace(microsecond=0)
        return due_utc.isoformat().replace("+00:00", "Z")

    def _build_trello_comment(self, *, task: ParsedTask, message: DiscordMessage, card_url: str) -> str:
        local_timestamp = message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        message_url = build_discord_message_url(
            guild_id=self.settings.discord_guild_id,
            channel_id=message.channel_id,
            message_id=message.id,
        )

        lines = [
            "Origem no Discord:",
            message_url,
            "",
            "Resumo interpretado:",
            f"- Tipo: {task.task_type.label_pt_br}",
            f"- Colaborador: {task.employee_name}",
            f"- Data: {task.effective_date.strftime('%d/%m/%Y')}",
            f"- Autor da mensagem: {message.author_name}",
            f"- Enviado em: {local_timestamp}",
            "",
            f"Card criado: {card_url}",
        ]

        if task.notes:
            lines.extend(["", "Observacoes detectadas:"])
            lines.extend(f"- {note}" for note in task.notes)

        lines.extend(["", "Mensagem original:", task.raw_excerpt])
        return "\n".join(lines)

    def _build_email_trello_comment(
        self,
        *,
        task: ParsedTask,
        email_message: EmailMessage,
        card_url: str,
    ) -> str:
        local_timestamp = email_message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        lines = [
            "Origem no e-mail:",
            f"- Conta: {self.settings.gmail_user_email}",
            f"- Remetente: {email_message.sender or 'Nao identificado'}",
            f"- Assunto: {email_message.subject or 'Sem assunto'}",
            f"- Enviado em: {local_timestamp}",
            f"- Gmail message ID: {email_message.id}",
            "",
            "Resumo interpretado:",
            f"- Tipo: {task.task_type.label_pt_br}",
            f"- Colaborador: {task.employee_name}",
            f"- Data: {task.effective_date.strftime('%d/%m/%Y')}",
            "",
            f"Card criado: {card_url}",
        ]

        if task.notes:
            lines.extend(["", "Informacoes adicionais detectadas:"])
            lines.extend(f"- {note}" for note in task.notes)

        lines.extend(["", "Conteudo original:", task.raw_excerpt])
        return "\n".join(lines)

    def _build_complement_trello_comment(
        self,
        *,
        card: TaskCard,
        message: DiscordMessage,
    ) -> str:
        local_timestamp = message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        message_url = build_discord_message_url(
            guild_id=self.settings.discord_guild_id,
            channel_id=message.channel_id,
            message_id=message.id,
        )
        lines = [
            "Complemento detectado no Discord:",
            message_url,
            "",
            "Card relacionado:",
            f"- Tipo: {card.task_type.label_pt_br}",
            f"- Colaborador: {card.employee_name}",
            f"- Data: {card.effective_date.strftime('%d/%m/%Y')}",
            f"- Autor da mensagem: {message.author_name}",
            f"- Enviado em: {local_timestamp}",
            "",
            "Informacao complementar:",
        ]
        lines.extend(f"- {note}" for note in _extract_complement_notes(message.content))
        return "\n".join(lines)

    def _build_requested_card_desc(
        self,
        *,
        requested_card: RequestedCard,
        command_message: DiscordMessage,
    ) -> str:
        local_timestamp = command_message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        prazo = requested_card.due_date.strftime("%d/%m/%Y") if requested_card.due_date else "Sem prazo definido"
        lines = [
            "**Resumo da solicitacao:**",
            requested_card.summary,
            "",
            f"**Solicitante:** {command_message.author_name}",
            f"**Prazo:** {prazo}",
            f"**Enviado em:** {local_timestamp}",
        ]
        context = requested_card.context_excerpt
        if context:
            lines.extend(["", "**Detalhes importantes:**", context])
        return "\n".join(lines)

    def _build_request_trello_comment(
        self,
        *,
        command_message: DiscordMessage,
        card_url: str,
    ) -> str:
        local_timestamp = command_message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        command_url = build_discord_message_url(
            guild_id=self.settings.discord_guild_id,
            channel_id=command_message.channel_id,
            message_id=command_message.id,
        )
        return f"Origem: {command_url}\nSolicitado por {command_message.author_name} em {local_timestamp}"

    def _build_discord_reply(self, card_urls: list[str]) -> str:
        if len(card_urls) == 1:
            return self.settings.discord_reply_template.format(card_url=card_urls[0])

        lines = ["Cards criados no Trello:"]
        lines.extend(f"- {card_url}" for card_url in card_urls)
        return "\n".join(lines)

    def _build_channel_modes(self) -> dict[str, set[str]]:
        channel_modes: dict[str, set[str]] = {}
        for channel_id in self.settings.discord_channel_ids:
            channel_modes.setdefault(channel_id, set()).add("structured")
        for channel_id in self.settings.discord_request_channel_ids:
            channel_modes.setdefault(channel_id, set()).add("request")
        return channel_modes

    def _load_recent_request_messages(
        self,
        *,
        command_message: DiscordMessage,
        channel_messages: list[DiscordMessage],
    ) -> list[DiscordMessage]:
        local_day_start = command_message.timestamp.astimezone(self.settings.timezone).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return [
            message
            for message in channel_messages
            if message.id != command_message.id
            and message.timestamp < command_message.timestamp
            and message.timestamp.astimezone(self.settings.timezone) >= local_day_start
        ]

    def _load_reply_chain_messages(self, message: DiscordMessage) -> list[DiscordMessage]:
        context_messages: list[DiscordMessage] = []
        current_channel_id = message.referenced_channel_id or message.channel_id
        current_message_id = message.referenced_message_id
        visited: set[tuple[str, str]] = set()

        while current_message_id and len(context_messages) < 20:
            key = (current_channel_id, current_message_id)
            if key in visited:
                break
            visited.add(key)
            context_message = self.discord.get_message(current_channel_id, current_message_id)
            context_messages.append(context_message)
            current_channel_id = context_message.referenced_channel_id or context_message.channel_id
            current_message_id = context_message.referenced_message_id

        context_messages.reverse()
        return context_messages


COMPLEMENT_PHRASES = (
    "nao precisa",
    "nao vai precisar",
    "sem notebook",
    "sem periferico",
    "sem perifericos",
    "precisa de",
    "vai precisar",
    "complemento",
    "alteracao",
    "alterar",
    "atualizar",
)


def _has_complement_signal(text: str) -> bool:
    normalized = _normalize_lookup(text)
    normalized_keywords = {_normalize_lookup(keyword) for keyword in NOTE_KEYWORDS}
    return any(keyword in normalized for keyword in normalized_keywords) or any(
        phrase in normalized for phrase in COMPLEMENT_PHRASES
    )


def _match_complement_cards(*, text: str, cards: list[TaskCard]) -> list[TaskCard]:
    normalized_text = _normalize_lookup(text)
    matched_cards: list[TaskCard] = []
    for card in cards:
        name_parts = _normalize_lookup(card.employee_name).split()
        if not name_parts:
            continue
        full_name = " ".join(name_parts)
        first_name = name_parts[0]
        last_name = name_parts[-1]
        date_matches = _text_mentions_date(normalized_text, card.effective_date)

        if full_name in normalized_text:
            matched_cards.append(card)
            continue
        if _contains_word(normalized_text, first_name) and (
            _contains_word(normalized_text, last_name) or date_matches
        ):
            matched_cards.append(card)

    return matched_cards


def _text_mentions_date(normalized_text: str, target_date: date) -> bool:
    day = target_date.day
    month = target_date.month
    year = target_date.year
    labels = {
        f"{day:02d}/{month:02d}/{year}",
        f"{day}/{month}/{year}",
        f"{day:02d}/{month:02d}",
        f"{day}/{month}",
        f"dia {day}",
        f"dia {day:02d}",
    }
    return any(label in normalized_text for label in labels)


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def _extract_complement_notes(text: str) -> list[str]:
    notes: list[str] = []
    for line in re.split(r"\n+", text):
        cleaned = line.strip(" -*\t")
        if not cleaned:
            continue
        if _has_complement_signal(cleaned):
            notes.append(_compact_comment_text(cleaned))
    if notes:
        return notes
    return [_compact_comment_text(text)]


def _compact_comment_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip(" ,")
    if len(compact) > 500:
        compact = compact[:497].rstrip() + "..."
    return compact


def _normalize_lookup(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text.casefold())
    without_accents = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"\s+", " ", without_accents).strip()
