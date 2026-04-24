from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

from .config import ConfirmationMode, Settings
from .discord_api import DiscordApiClient, build_discord_message_url
from .http import ApiError
from .models import DiscordMessage, ParsedTask, RunSummary
from .parser import TaskParser
from .trello_api import TrelloApiClient


LOGGER = logging.getLogger(__name__)

SUPPORTED_MESSAGE_TYPES = {0, 19}


class DiscordTrelloService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discord = DiscordApiClient(settings)
        self.trello = TrelloApiClient(settings)
        self.parser = TaskParser(settings)

    def run(self) -> RunSummary:
        summary = RunSummary()
        cutoff = datetime.now(tz=self.settings.timezone) - timedelta(days=self.settings.lookback_days)

        for channel_id in self.settings.discord_channel_ids:
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
                    bot_reply_reference_ids=bot_reply_reference_ids,
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

        return summary

    def _process_message(
        self,
        message: DiscordMessage,
        *,
        bot_reply_reference_ids: set[str],
    ) -> tuple[str, int]:
        if not self._should_consider_message(message):
            return "skipped", 0

        if message.has_confirmation_reaction(self.settings.discord_reaction_emoji):
            LOGGER.info("Mensagem %s ja possui confirmacao do bot.", message.id)
            return "already_confirmed", 0
        if message.id in bot_reply_reference_ids:
            LOGGER.info("Mensagem %s ja possui resposta de confirmacao do bot.", message.id)
            return "already_confirmed", 0

        parse_result = self.parser.parse_message(message)
        if not parse_result.tasks:
            LOGGER.info("Mensagem %s ignorada: %s.", message.id, parse_result.reason)
            return "skipped", 0

        LOGGER.info("Mensagem %s gerou %s tarefa(s).", message.id, len(parse_result.tasks))

        try:
            card_urls: list[str] = []
            for task in parse_result.tasks:
                LOGGER.info(
                    "Tarefa detectada na mensagem %s: %s %s em %s.",
                    message.id,
                    task.task_type.value,
                    task.employee_name,
                    task.effective_date.isoformat(),
                )
                card = self.trello.create_card_from_template(
                    card_name=self._build_card_name(task),
                    due_iso=self._build_due_iso(task.effective_date),
                    task_type=task.task_type,
                )

                card_id = str(card["id"])
                card_url = str(card["url"])
                card_urls.append(card_url)
                self.trello.add_comment(
                    card_id=card_id,
                    text=self._build_trello_comment(task=task, message=message, card_url=card_url),
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
                    content=self._build_discord_reply(card_urls),
                )

            return "created", len(card_urls)
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao processar a mensagem %s.", message.id)
            return "error", 0

    def _should_consider_message(self, message: DiscordMessage) -> bool:
        if message.author_is_bot or message.webhook_id:
            return False
        if message.message_type not in SUPPORTED_MESSAGE_TYPES:
            return False
        if not message.content:
            return False
        return True

    def _build_card_name(self, task: ParsedTask) -> str:
        date_label = task.effective_date.strftime("%d/%m/%Y")
        return f"[{task.task_type.label_pt_br}] {task.employee_name} - {date_label}"

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

    def _build_discord_reply(self, card_urls: list[str]) -> str:
        if len(card_urls) == 1:
            return self.settings.discord_reply_template.format(card_url=card_urls[0])

        lines = ["Cards criados no Trello:"]
        lines.extend(f"- {card_url}" for card_url in card_urls)
        return "\n".join(lines)
