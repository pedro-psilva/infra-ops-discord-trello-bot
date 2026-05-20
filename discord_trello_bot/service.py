from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone as _tz
from email.utils import getaddresses

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
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)
PAST_TASK_GRACE_DAYS = 3
COMPACT_COMMENT_MAX_LEN = 500
ONBOARDING_EMAIL_DETAILS_HEADING = "## Informacoes de onboarding recebidas por e-mail"
CARGO_QUESTION_MARKER = "[cargo?]"
CARGO_QUESTION_TEMPLATE = (
    "Card criado para **{name}** em **{date}** \u2705\n"
    "N\u00e3o identifiquei o cargo desta pessoa. Qual \u00e9 o cargo? {marker}\n"
    "_(Responda a esta mensagem com apenas o cargo)_"
)


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
        bot_user_id: str | None = None
        bot_role_ids: set[str] = set()
        if self.settings.discord_request_channel_ids:
            try:
                bot_user_id = self.discord.get_current_user_id()
                bot_role_ids = set(self.discord.get_current_member_role_ids())
            except ApiError:
                LOGGER.warning(
                    "Nao foi possivel obter o user/roles do bot (rate limit?). "
                    "Processamento continua sem filtrar mensagens proprias."
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

            # Processa respostas de cargo antes do loop principal
            cargo_reply_ids: set[str] = set()
            if "structured" in modes:
                cargo_reply_ids = self._process_cargo_reply_messages(
                    messages=messages,
                    channel_id=channel_id,
                    summary=summary,
                )

            for message in messages:
                if message.id in cargo_reply_ids:
                    continue
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
            cargo_missing_tasks: list[ParsedTask] = []
            for task in parse_result.tasks:
                if self._is_stale_task(task):
                    LOGGER.info(
                        "Tarefa da mensagem %s ignorada por estar alem da tolerancia de passado: %s %s.",
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
                card, was_created = self._get_or_create_task_card(task)
                if was_created:
                    created_count += 1
                card_id = str(card["id"])
                card_url = str(card["url"])
                card_urls.append(card_url)
                self.trello.add_comment(
                    card_id=card_id,
                    text=self._build_trello_comment(task=task, message=message),
                )
                # Atualiza descricao do card com cargo se disponivel
                if task.cargo:
                    self._set_cargo_in_card_description(card_id, task.cargo)
                elif task.task_type is TaskType.ONBOARDING and was_created:
                    cargo_missing_tasks.append(task)

            if not card_urls:
                return "skipped", 0

            self.discord.add_reaction(
                channel_id=message.channel_id,
                message_id=message.id,
                emoji=self.settings.discord_reaction_emoji,
            )

            # Pergunta cargo para tarefas sem cargo detectado (onboarding novo)
            for task in cargo_missing_tasks:
                try:
                    self.discord.reply_to_message(
                        channel_id=message.channel_id,
                        message_id=message.id,
                        content=CARGO_QUESTION_TEMPLATE.format(
                            name=task.employee_name,
                            date=task.effective_date.strftime("%d/%m/%Y"),
                            marker=CARGO_QUESTION_MARKER,
                        ),
                    )
                    LOGGER.info(
                        "Pergunta de cargo postada para %s (mensagem %s).",
                        task.employee_name,
                        message.id,
                    )
                except (ApiError, ValueError):
                    LOGGER.warning(
                        "Falha ao postar pergunta de cargo para %s.",
                        task.employee_name,
                    )

            # Confirmacao normal apenas quando nao ha cargo pendente
            if (
                not cargo_missing_tasks
                and self.settings.discord_confirmation_mode in {ConfirmationMode.REPLY, ConfirmationMode.BOTH}
            ):
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
                    context_messages=selected_context_messages,
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
                if self.settings.gmail_backfill_onboarding_descriptions:
                    updated_count = self._backfill_onboarding_description_from_email(email_message)
                    if updated_count:
                        LOGGER.info(
                            "E-mail %s usado para atualizar descricao de %s card(s) de onboarding.",
                            email_message.id,
                            updated_count,
                        )
                    else:
                        LOGGER.info("E-mail %s ja processado, sem backfill aplicavel.", email_message.id)
                    continue
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
        # Respostas e encaminhamentos nunca criam card novo — apenas sincronizam
        # detalhes em cards existentes se houver match, e sempre sao marcados como processados.
        if _is_email_reply_or_forward(email_message.subject):
            updated_count = self._sync_onboarding_details_email_to_existing_cards(email_message)
            if updated_count:
                LOGGER.info(
                    "E-mail %s (resposta/encaminhamento) atualizou descricao de %s card(s) de onboarding existente(s).",
                    email_message.id,
                    updated_count,
                )
                if self.gmail is not None:
                    try:
                        self.gmail.mark_processed(email_message.id)
                    except Exception:
                        LOGGER.warning(
                            "Falha ao marcar e-mail %s como processado. Sera reprocessado na proxima execucao.",
                            email_message.id,
                        )
                return "created", 0
            LOGGER.info(
                "E-mail %s ignorado: resposta/encaminhamento nao abre card novo.",
                email_message.id,
            )
            if self.gmail is not None:
                try:
                    self.gmail.mark_processed(email_message.id)
                except Exception:
                    LOGGER.warning(
                        "Falha ao marcar e-mail %s como processado. Sera reprocessado na proxima execucao.",
                        email_message.id,
                    )
            return "skipped", 0

        synthetic_message = self._build_email_task_message(email_message)
        parse_result = self.parser.parse_message(synthetic_message)
        if not parse_result.tasks:
            updated_count = self._sync_onboarding_details_email_to_existing_cards(email_message)
            if updated_count:
                LOGGER.info(
                    "E-mail %s atualizou descricao de %s card(s) de onboarding existente(s).",
                    email_message.id,
                    updated_count,
                )
                if self.gmail is not None:
                    self.gmail.mark_processed(email_message.id)
                return "created", 0
            LOGGER.info("E-mail %s ignorado: %s.", email_message.id, parse_result.reason)
            return "skipped", 0

        if _should_treat_email_as_onboarding_details_only(email_message, parse_result.tasks):
            updated_count = self._sync_onboarding_details_email_to_existing_cards(email_message)
            if updated_count:
                LOGGER.info(
                    "E-mail %s tratado como complemento e atualizou %s card(s) de onboarding existente(s).",
                    email_message.id,
                    updated_count,
                )
                if self.gmail is not None:
                    try:
                        self.gmail.mark_processed(email_message.id)
                    except Exception:
                        LOGGER.warning(
                            "Falha ao marcar e-mail %s como processado. Sera reprocessado na proxima execucao.",
                            email_message.id,
                        )
                return "created", 0
            LOGGER.info(
                "E-mail %s ignorado: contem detalhes de onboarding, mas nao tem sinal forte "
                "de criacao nem card futuro compativel.",
                email_message.id,
            )
            return "skipped", 0

        if _should_treat_email_as_offboarding_reply_only(email_message, parse_result.tasks):
            LOGGER.info(
                "E-mail %s ignorado: resposta/encaminhamento de offboarding nao abre card novo.",
                email_message.id,
            )
            if self.gmail is not None:
                try:
                    self.gmail.mark_processed(email_message.id)
                except Exception:
                    LOGGER.warning(
                        "Falha ao marcar e-mail %s como processado. Sera reprocessado na proxima execucao.",
                        email_message.id,
                    )
            return "skipped", 0

        try:
            processed_count = 0
            created_count = 0
            for task in parse_result.tasks:
                if self._is_stale_task(task):
                    LOGGER.info(
                        "Tarefa do e-mail %s ignorada por estar alem da tolerancia de passado: %s %s.",
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
                    ),
                )
                self._sync_email_details_to_card_description(
                    card_id=str(card["id"]),
                    task=task,
                    email_message=email_message,
                )
                processed_count += 1

            if processed_count == 0:
                return "skipped", 0

            if self.gmail is not None:
                try:
                    self.gmail.mark_processed(email_message.id)
                except Exception:
                    LOGGER.warning(
                        "Falha ao marcar e-mail %s como processado. Sera reprocessado na proxima execucao.",
                        email_message.id,
                    )
            return "created", created_count
        except (ApiError, ValueError):
            LOGGER.exception("Falha ao processar e-mail %s.", email_message.id)
            return "error", 0

    def _backfill_onboarding_description_from_email(self, email_message: EmailMessage) -> int:
        synthetic_message = self._build_email_task_message(email_message)
        parse_result = self.parser.parse_message(synthetic_message)
        if not parse_result.tasks:
            return self._sync_onboarding_details_email_to_existing_cards(email_message)

        updated_count = 0
        for task in parse_result.tasks:
            if (
                task.task_type is not TaskType.ONBOARDING
                or not _extract_onboarding_email_description_details(email_message.body)
            ):
                continue
            existing_card = self._find_existing_task_card(task)
            if existing_card is None:
                LOGGER.info(
                    "Backfill ignorado para e-mail %s: card existente nao encontrado para %s %s.",
                    email_message.id,
                    task.employee_name,
                    task.effective_date.isoformat(),
                )
                continue
            self._sync_email_details_to_card_description(
                card_id=str(existing_card["id"]),
                task=task,
                email_message=email_message,
            )
            updated_count += 1
        return updated_count

    def _sync_onboarding_details_email_to_existing_cards(self, email_message: EmailMessage) -> int:
        if not _extract_onboarding_email_description_details(email_message.body):
            return 0

        today = datetime.now(tz=self.settings.timezone).date()
        future_onboarding_cards = [
            card
            for card in self.trello.list_open_task_cards(TaskType.ONBOARDING)
            if card.effective_date >= today
        ]
        matched_cards = _match_complement_cards(
            text=f"{email_message.subject}\n{email_message.body}",
            cards=future_onboarding_cards,
        )
        updated_count = 0
        for card in matched_cards:
            task = ParsedTask(
                task_type=TaskType.ONBOARDING,
                employee_name=card.employee_name,
                effective_date=card.effective_date,
                notes=tuple(_extract_onboarding_email_description_details(email_message.body)),
                raw_excerpt=email_message.body[:1800],
            )
            if self._sync_email_details_to_card_description(
                card_id=card.id,
                task=task,
                email_message=email_message,
            ):
                updated_count += 1
        return updated_count

    def _get_or_create_task_card(self, task: ParsedTask) -> tuple[dict, bool]:
        existing_card = self._find_existing_task_card(task)
        if existing_card is not None:
            return existing_card, False

        card = self.trello.create_card_from_template(
            card_name=self._build_card_name(task),
            due_iso=self._build_due_iso(task.effective_date),
            task_type=task.task_type,
        )
        return card, True

    def _find_existing_task_card(self, task: ParsedTask) -> dict | None:
        card_name = self._build_card_name(task)
        existing_card = self.trello.find_open_card_by_name(card_name)
        if existing_card is not None:
            LOGGER.info("Card existente encontrado no Trello: %s.", card_name)
            return existing_card

        compatible_card = self._find_compatible_task_card(task)
        if compatible_card is not None:
            LOGGER.info(
                "Card existente compativel encontrado no Trello: %s -> %s.",
                card_name,
                compatible_card.name,
            )
            return {
                "id": compatible_card.id,
                "url": compatible_card.url,
                "name": compatible_card.name,
            }
        return None

    def _find_compatible_task_card(self, task: ParsedTask) -> TaskCard | None:
        same_date_cards = [
            card
            for card in self.trello.list_open_task_cards(task.task_type)
            if card.effective_date == task.effective_date
        ]
        for card in same_date_cards:
            if _employee_names_match(task.employee_name, card.employee_name):
                return card

        first_name_matches = [
            card
            for card in same_date_cards
            if _single_first_name_match(task.employee_name, card.employee_name)
        ]
        if len(first_name_matches) == 1:
            return first_name_matches[0]
        return None

    def _is_stale_task(self, task: ParsedTask) -> bool:
        today = datetime.now(tz=self.settings.timezone).date()
        oldest_allowed_date = today - timedelta(days=self.settings.lookback_days)
        return task.effective_date < oldest_allowed_date

    def _build_email_task_message(self, email_message: EmailMessage) -> DiscordMessage:
        content = "\n".join(
            part
            for part in (
                email_message.subject,
                _build_email_recipient_line(
                    email_message.recipient,
                    account_email=self.settings.gmail_user_email,
                ),
                email_message.body,
            )
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

    def _build_trello_comment(self, *, task: ParsedTask, message: DiscordMessage) -> str:
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
        ]

        if task.cargo:
            lines.append(f"- Cargo: {task.cargo}")

        if task.notes:
            lines.extend(["", "Observacoes detectadas:"])
            lines.extend(f"- {note}" for note in task.notes)

        return "\n".join(lines)

    def _build_email_trello_comment(
        self,
        *,
        task: ParsedTask,
        email_message: EmailMessage,
    ) -> str:
        local_timestamp = email_message.timestamp.astimezone(self.settings.timezone).strftime("%d/%m/%Y %H:%M")
        lines = [
            "Origem no e-mail:",
            f"- Conta: {self.settings.gmail_user_email}",
            f"- Remetente: {email_message.sender or 'Nao identificado'}",
            f"- Destinatario: {email_message.recipient or 'Nao identificado'}",
            f"- Assunto: {email_message.subject or 'Sem assunto'}",
            f"- Enviado em: {local_timestamp}",
            f"- Gmail message ID: {email_message.id}",
            "",
            "Resumo interpretado:",
            f"- Tipo: {task.task_type.label_pt_br}",
            f"- Colaborador: {task.employee_name}",
            f"- Data: {task.effective_date.strftime('%d/%m/%Y')}",
        ]

        if (
            task.task_type is TaskType.ONBOARDING
            and _extract_onboarding_email_description_details(email_message.body)
        ):
            lines.extend(["", "Informacoes adicionais adicionadas na descricao do card."])
        elif task.notes:
            lines.extend(["", "Informacoes adicionais detectadas:"])
            lines.extend(f"- {note}" for note in task.notes)
        return "\n".join(lines)

    def _sync_email_details_to_card_description(
        self,
        *,
        card_id: str,
        task: ParsedTask,
        email_message: EmailMessage,
    ) -> bool:
        if task.task_type is not TaskType.ONBOARDING:
            return False

        card = self.trello.get_card(card_id, fields="id,desc")
        current_desc = str(card.get("desc") or "")
        updated_desc = _upsert_onboarding_email_details_section(
            current_desc=current_desc,
            task=task,
            email_message=email_message,
            timezone=self.settings.timezone,
        )
        if updated_desc != current_desc:
            self.trello.update_card_description(card_id=card_id, desc=updated_desc)
            return True
        return False

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
        context_messages: list[DiscordMessage] | None = None,
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

        cited_links = _extract_urls_from_messages([command_message, *(context_messages or [])])
        if cited_links:
            lines.extend(["", "**Links citados:**"])
            lines.extend(f"- {link}" for link in cited_links)
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

    def _set_cargo_in_card_description(self, card_id: str, cargo: str) -> None:
        """Insere o cargo na primeira linha da descricao do card se ainda nao estiver la."""
        try:
            trello_card = self.trello.get_card(card_id, fields="id,desc")
            current_desc = str(trello_card.get("desc") or "")
            cargo_line = f"**Cargo:** {cargo}"
            if cargo_line in current_desc:
                return
            new_desc = f"{cargo_line}\n\n{current_desc}".strip() if current_desc else cargo_line
            self.trello.update_card_description(card_id=card_id, desc=new_desc)
        except (ApiError, ValueError):
            LOGGER.warning("Falha ao atualizar cargo no card %s.", card_id)

    def _process_cargo_reply_messages(
        self,
        messages: list[DiscordMessage],
        channel_id: str,
        summary: RunSummary,
    ) -> set[str]:
        """Detecta respostas humanas as perguntas de cargo do bot e atualiza os cards.

        Retorna o conjunto de IDs de mensagens processadas como respostas de cargo.
        """
        # Coleta mensagens do bot com a marca de pergunta de cargo
        cargo_questions: dict[str, str] = {}  # bot_msg_id -> employee_name
        for msg in messages:
            if not msg.author_is_bot:
                continue
            if CARGO_QUESTION_MARKER not in msg.content:
                continue
            name_match = re.search(r"para \*\*(.+?)\*\*", msg.content)
            if name_match:
                cargo_questions[msg.id] = name_match.group(1)

        if not cargo_questions:
            return set()

        processed_ids: set[str] = set()
        for msg in messages:
            if msg.author_is_bot:
                continue
            if msg.referenced_message_id not in cargo_questions:
                continue
            # Ja processado anteriormente
            if msg.has_confirmation_reaction(self.settings.discord_reaction_emoji):
                processed_ids.add(msg.id)
                continue

            employee_name = cargo_questions[msg.referenced_message_id]
            cargo = msg.content.strip()
            if not cargo or len(cargo.split()) > 10:
                processed_ids.add(msg.id)
                continue

            try:
                updated = self._apply_cargo_to_matching_card(
                    employee_name=employee_name,
                    cargo=cargo,
                )
                if updated:
                    self.discord.add_reaction(
                        channel_id=channel_id,
                        message_id=msg.id,
                        emoji=self.settings.discord_reaction_emoji,
                    )
                    LOGGER.info(
                        "Cargo '%s' aplicado ao card de %s via resposta Discord %s.",
                        cargo,
                        employee_name,
                        msg.id,
                    )
                else:
                    LOGGER.warning(
                        "Cargo recebido para %s, mas nenhum card aberto encontrado.",
                        employee_name,
                    )
            except (ApiError, ValueError):
                LOGGER.exception(
                    "Falha ao aplicar cargo para %s a partir da mensagem %s.",
                    employee_name,
                    msg.id,
                )
                summary.errors += 1

            processed_ids.add(msg.id)

        return processed_ids

    def _apply_cargo_to_matching_card(self, *, employee_name: str, cargo: str) -> bool:
        """Encontra o card de onboarding do colaborador e atualiza com o cargo.

        Retorna True se algum card foi atualizado.
        """
        cards = self.trello.list_open_task_cards(TaskType.ONBOARDING)
        for card in cards:
            if _employee_names_match(employee_name, card.employee_name):
                self._set_cargo_in_card_description(card.id, cargo)
                return True
        return False

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
            if context_message is None:
                break
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
NAME_CONNECTOR_WORDS = {"da", "das", "de", "do", "dos", "e"}
ONBOARDING_CREATION_SUBJECT_PHRASES = (
    "onboarding",
    "admissao",
    "contratacao",
    "novo colaborador",
    "nova colaboradora",
    "solicitacao de equipamentos",
    "kit de boas-vindas",
    "boas-vindas",
)
ONBOARDING_DATE_FIELD_PHRASES = (
    "data de admissao",
    "data de entrada",
    "data de inicio",
    "data de ingresso",
    "admissao:",
    "admissao -",
    "entrada:",
    "entrada -",
    "inicio:",
    "inicio -",
    "ingresso:",
    "data:",
)
ONBOARDING_IDENTITY_FIELD_PHRASES = (
    "nome completo:",
    "nome:",
    "colaborador:",
    "colaboradora:",
    "funcionario:",
    "funcionaria:",
    "dados do colaborador",
    "dados da colaboradora",
    "informacoes do colaborador",
    "informacoes da colaboradora",
)
REPLY_FORWARD_SUBJECT_PATTERN = re.compile(r"\b(?:re|res|fw|fwd|enc)\s*:", re.IGNORECASE)


def _has_complement_signal(text: str) -> bool:
    normalized = _normalize_lookup(text)
    normalized_keywords = {_normalize_lookup(keyword) for keyword in NOTE_KEYWORDS}
    return any(keyword in normalized for keyword in normalized_keywords) or any(
        phrase in normalized for phrase in COMPLEMENT_PHRASES
    )


def _should_treat_email_as_onboarding_details_only(
    email_message: EmailMessage,
    tasks: tuple[ParsedTask, ...],
) -> bool:
    if not any(task.task_type is TaskType.ONBOARDING for task in tasks):
        return False
    if not _extract_onboarding_email_description_details(email_message.body):
        return False
    return not _has_strong_onboarding_email_creation_signal(email_message)


def _has_strong_onboarding_email_creation_signal(email_message: EmailMessage) -> bool:
    normalized_subject = _normalize_lookup(email_message.subject)
    normalized_body = _normalize_lookup(email_message.body)
    subject_says_creation = any(
        phrase in normalized_subject
        for phrase in ONBOARDING_CREATION_SUBJECT_PHRASES
    )
    body_has_date = any(phrase in normalized_body for phrase in ONBOARDING_DATE_FIELD_PHRASES)
    body_has_identity = any(phrase in normalized_body for phrase in ONBOARDING_IDENTITY_FIELD_PHRASES)

    if body_has_identity and body_has_date:
        return True
    return subject_says_creation and body_has_date


def _should_treat_email_as_offboarding_reply_only(
    email_message: EmailMessage,
    tasks: tuple[ParsedTask, ...],
) -> bool:
    if not any(task.task_type is TaskType.OFFBOARDING for task in tasks):
        return False
    return _is_email_reply_or_forward(email_message.subject)


def _is_email_reply_or_forward(subject: str) -> bool:
    return REPLY_FORWARD_SUBJECT_PATTERN.search(_normalize_lookup(subject)) is not None


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


def _extract_urls_from_messages(messages: list[DiscordMessage]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for match in URL_PATTERN.finditer(message.content):
            url = match.group(0).rstrip(".,;:!?)]}'\"")
            if not url:
                continue
            if url.startswith("www."):
                url = f"https://{url}"
            normalized = url.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(url)
    return urls


def _build_email_recipient_line(recipient: str, *, account_email: str | None = None) -> str:
    if account_email and _extract_email_address(recipient).casefold() == account_email.strip().casefold():
        return ""
    recipient_name = _extract_email_display_name(recipient)
    if not recipient_name:
        return ""
    return f"Destinatario: {recipient_name}"


def _extract_email_address(value: str) -> str:
    for _name, address in getaddresses([value]):
        if address:
            return address.strip()
    return ""


def _extract_email_display_name(value: str) -> str:
    for display_name, address in getaddresses([value]):
        cleaned_name = display_name.strip().strip('"')
        if cleaned_name:
            return cleaned_name
        if address:
            return _display_name_from_email_address(address)

    cleaned = value.strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*<[^>]+>\s*$", "", cleaned).strip().strip('"')
    if "@" in cleaned:
        return _display_name_from_email_address(cleaned)
    return cleaned


def _display_name_from_email_address(address: str) -> str:
    local_part = address.strip().split("@", 1)[0]
    pieces = [piece for piece in re.split(r"[._+-]+", local_part) if piece]
    if len(pieces) < 2:
        return ""
    return " ".join(piece.capitalize() for piece in pieces)


def _compact_comment_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip(" ,")
    if len(compact) > COMPACT_COMMENT_MAX_LEN:
        compact = compact[:COMPACT_COMMENT_MAX_LEN - 3].rstrip() + "..."
    return compact


def _normalize_lookup(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text.casefold())
    without_accents = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"\s+", " ", without_accents).strip()


def _name_tokens(name: str) -> list[str]:
    normalized = _normalize_lookup(name)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    return [
        token
        for token in re.split(r"\s+", normalized)
        if token and token not in NAME_CONNECTOR_WORDS
    ]


def _employee_names_match(left: str, right: str) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    if left_tokens[0] != right_tokens[0]:
        return False

    left_set = set(left_tokens)
    right_set = set(right_tokens)
    smaller = left_set if len(left_set) <= len(right_set) else right_set
    larger = right_set if smaller is left_set else left_set
    if len(smaller) >= 2 and smaller.issubset(larger):
        return True

    overlap = len(left_set & right_set)
    return overlap >= 2 and overlap / min(len(left_set), len(right_set)) >= 0.8


def _single_first_name_match(left: str, right: str) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens) == 1 and left_tokens[0] == right_tokens[0]


def _upsert_onboarding_email_details_section(
    *,
    current_desc: str,
    task: ParsedTask,
    email_message: EmailMessage,
    timezone,
) -> str:
    notes = _unique_compact_lines(
        tuple(_extract_onboarding_email_description_details(email_message.body))
    )
    if not notes:
        return current_desc

    local_timestamp = email_message.timestamp.astimezone(timezone).strftime("%d/%m/%Y %H:%M")
    section_lines = [
        ONBOARDING_EMAIL_DETAILS_HEADING,
        f"- Colaborador: {task.employee_name}",
        f"- Data de entrada: {task.effective_date.strftime('%d/%m/%Y')}",
        f"- Recebido por e-mail em: {local_timestamp}",
        "",
        "**Dados recebidos:**",
    ]
    section_lines.extend(f"- {note}" for note in notes)
    section = "\n".join(section_lines)

    pattern = re.compile(
        rf"(?:\n{{2,}})?{re.escape(ONBOARDING_EMAIL_DETAILS_HEADING)}\n.*?(?=\n{{2,}}## |\Z)",
        re.DOTALL,
    )
    if pattern.search(current_desc):
        return pattern.sub(f"\n\n{section}", current_desc).strip()
    if current_desc.strip():
        return f"{current_desc.rstrip()}\n\n{section}"
    return section


def _unique_compact_lines(values: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = _compact_comment_text(value)
        key = _normalize_lookup(compact)
        if not compact or key in seen:
            continue
        seen.add(key)
        lines.append(compact)
    return lines


def _extract_onboarding_email_description_details(body: str) -> list[str]:
    details: list[str] = []
    field_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?"
        r"(?P<label>"
        r"endere[c\u00e7]o(?:\s+(?:de\s+entrega|completo))?|"
        r"logradouro|rua|avenida|bairro|cidade|cep|n[u\u00fa]mero|numero|complemento|"
        r"telefone|celular|cargo|[a\u00e1]rea|gestor|l[i\u00ed]der|modalidade|"
        r"notebook(?:\s+e\s+perif[e\u00e9]ricos)?|perif[e\u00e9]ricos"
        r")\s*[:\-]\s*(?P<value>.+)$",
        re.IGNORECASE,
    )
    address_line_pattern = re.compile(
        r"^\s*(?:rua|r\.|avenida|av\.|alameda|travessa)\b.+\d+",
        re.IGNORECASE,
    )

    for line in re.split(r"\n+", body):
        cleaned = line.strip(" -*\t")
        if not cleaned:
            continue
        field_match = field_pattern.match(cleaned)
        if field_match:
            label = field_match.group("label").strip()
            value = field_match.group("value").strip()
            if value:
                details.append(f"{label}: {value}")
            continue
        if address_line_pattern.match(cleaned):
            details.append(cleaned)
    return details
