from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord

from .config import Settings
from .http import ApiError
from .models import DiscordMessage, DiscordReaction, RunSummary
from .service import DiscordTrelloService


LOGGER = logging.getLogger(__name__)


class InfraOpsDiscordClient(discord.Client):
    def __init__(self, *, settings: Settings, service: DiscordTrelloService) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.service = service
        self._bot_user_id: str | None = None
        self._bot_role_ids: set[str] | None = None
        self._dm_latest_message: dict[str, str] = {}
        self._dm_locks: dict[str, asyncio.Lock] = {}

    async def on_ready(self) -> None:
        if self.user is not None:
            self._bot_user_id = str(self.user.id)
        LOGGER.info("Infra Ops conectado ao Discord como %s.", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Mensagem direta (DM): sem servidor associado.
        if message.guild is None:
            await self._handle_direct_message(message)
            return

        channel_id = str(message.channel.id)
        modes = self._channel_modes(channel_id)
        if not modes:
            return

        incoming_message = _message_from_discord_py(message)
        if "request" in modes:
            try:
                mentioned = self._mentions_bot_or_role(incoming_message)
            except ApiError:
                LOGGER.warning(
                    "Nao foi possivel resolver bot/roles para a mensagem %s; ignorando.",
                    message.id,
                )
                return
            if not mentioned:
                return

        try:
            outcome, created_count = await asyncio.to_thread(
                self._process_live_message,
                incoming_message,
                modes,
            )
        except Exception:
            LOGGER.exception("Falha ao processar mensagem em tempo real %s.", message.id)
            return

        LOGGER.info(
            "Mensagem em tempo real %s processada. resultado=%s criados=%s",
            message.id,
            outcome,
            created_count,
        )

    async def _handle_direct_message(self, message: discord.Message) -> None:
        allowed_user_ids = self.settings.discord_dm_allowed_user_ids
        if not allowed_user_ids:
            return  # Recurso de DM desativado enquanto nao houver allowlist.

        author_id = str(message.author.id)
        if author_id not in allowed_user_ids:
            LOGGER.info("DM de usuario nao autorizado %s ignorada.", author_id)
            return

        channel_id = str(message.channel.id)
        self._dm_latest_message[channel_id] = str(message.id)
        asyncio.create_task(self._debounced_direct_message(message))

    async def _debounced_direct_message(self, message: discord.Message) -> None:
        channel_id = str(message.channel.id)
        message_id = str(message.id)
        await asyncio.sleep(self.settings.discord_dm_debounce_seconds)
        if self._dm_latest_message.get(channel_id) != message_id:
            return

        lock = self._dm_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            if self._dm_latest_message.get(channel_id) != message_id:
                return
            incoming_message = _message_from_discord_py(message)
            try:
                outcome, created_count = await asyncio.to_thread(
                    self._process_direct_message,
                    incoming_message,
                )
            except Exception:
                LOGGER.exception("Falha ao processar DM %s.", message.id)
                return

        LOGGER.info(
            "DM %s processada. resultado=%s criados=%s",
            message.id,
            outcome,
            created_count,
        )

    def _process_direct_message(self, message: DiscordMessage) -> tuple[str, int]:
        # Busca o historico recente da DM para tratar respostas de cargo
        # (humano respondendo a pergunta do bot dentro da propria DM).
        cutoff = message.timestamp.astimezone(self.settings.timezone) - timedelta(
            days=self.settings.lookback_days
        )
        try:
            dm_messages = self.service.discord.iter_messages_since(message.channel_id, cutoff)
        except Exception:
            LOGGER.warning("Nao foi possivel ler o historico da DM %s.", message.channel_id)
            dm_messages = []
        dm_messages = _merge_current_message(dm_messages, message)

        cargo_reply_ids = self.service._process_cargo_reply_messages(
            messages=dm_messages,
            channel_id=message.channel_id,
            summary=RunSummary(),
        )
        if message.id in cargo_reply_ids:
            return "cargo_reply", 0

        return self.service.process_direct_message(message, thread_messages=dm_messages)

    def _channel_modes(self, channel_id: str) -> set[str]:
        modes: set[str] = set()
        if channel_id in self.settings.discord_channel_ids:
            modes.add("structured")
        if channel_id in self.settings.discord_request_channel_ids:
            modes.add("request")
        return modes

    def _mentions_bot_or_role(self, message: DiscordMessage) -> bool:
        bot_user_id = self._current_bot_user_id()
        bot_role_ids = self._current_bot_role_ids()
        return message.mentions_user(bot_user_id) or message.mentions_any_role(bot_role_ids)

    def _process_live_message(
        self,
        message: DiscordMessage,
        modes: set[str],
    ) -> tuple[str, int]:
        cutoff = message.timestamp.astimezone(self.settings.timezone) - timedelta(
            days=self.settings.lookback_days
        )
        channel_messages = self.service.discord.iter_messages_since(
            message.channel_id,
            cutoff,
        )
        channel_messages = _merge_current_message(channel_messages, message)

        # Respostas de cargo (humano respondendo a pergunta do bot) sao tratadas
        # antes do fluxo normal. No modo listener isso e essencial: sem isso, a
        # resposta com o cargo nunca era aplicada ao card.
        cargo_reply_ids = self.service._process_cargo_reply_messages(
            messages=channel_messages,
            channel_id=message.channel_id,
            summary=RunSummary(),
        )
        if message.id in cargo_reply_ids:
            return "cargo_reply", 0

        bot_reply_reference_ids = {
            item.referenced_message_id
            for item in channel_messages
            if item.author_is_bot and item.referenced_message_id
        }
        return self.service._process_message(
            message,
            channel_messages=channel_messages,
            bot_reply_reference_ids=bot_reply_reference_ids,
            modes=modes,
            bot_user_id=self._current_bot_user_id(),
            bot_role_ids=self._current_bot_role_ids(),
        )

    def _current_bot_user_id(self) -> str:
        if self._bot_user_id is None:
            self._bot_user_id = self.service.discord.get_current_user_id()
        return self._bot_user_id

    def _current_bot_role_ids(self) -> set[str]:
        if self._bot_role_ids is None:
            self._bot_role_ids = set(self.service.discord.get_current_member_role_ids())
        return self._bot_role_ids


def run_live_bot(settings: Settings) -> None:
    service = DiscordTrelloService(settings)
    client = InfraOpsDiscordClient(settings=settings, service=service)
    client.run(settings.discord_bot_token)


def _message_from_discord_py(message: discord.Message) -> DiscordMessage:
    return DiscordMessage(
        id=str(message.id),
        channel_id=str(message.channel.id),
        content=(message.content or "").strip(),
        timestamp=message.created_at,
        author_id=str(message.author.id),
        author_name=_author_name(message.author),
        author_is_bot=message.author.bot,
        message_type=int(message.type.value),
        webhook_id=str(message.webhook_id) if message.webhook_id is not None else None,
        referenced_channel_id=_referenced_channel_id(message),
        referenced_message_id=_referenced_message_id(message),
        mentioned_user_ids=tuple(str(user.id) for user in message.mentions),
        mentioned_role_ids=tuple(str(role.id) for role in message.role_mentions),
        reactions=tuple(_reaction_from_discord_py(reaction) for reaction in message.reactions),
    )


def _author_name(author: discord.abc.User) -> str:
    global_name = getattr(author, "global_name", None)
    display_name = getattr(author, "display_name", None)
    return str(global_name or display_name or author.name or "desconhecido")


def _referenced_channel_id(message: discord.Message) -> str | None:
    reference = message.reference
    if reference is None or reference.channel_id is None:
        return None
    return str(reference.channel_id)


def _referenced_message_id(message: discord.Message) -> str | None:
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    return str(reference.message_id)


def _reaction_from_discord_py(reaction: discord.Reaction) -> DiscordReaction:
    emoji = reaction.emoji
    if isinstance(emoji, str):
        return DiscordReaction(emoji_name=emoji, emoji_id=None, me=bool(reaction.me))
    return DiscordReaction(
        emoji_name=emoji.name,
        emoji_id=str(emoji.id) if emoji.id is not None else None,
        me=bool(reaction.me),
    )


def _merge_current_message(
    channel_messages: list[DiscordMessage],
    message: DiscordMessage,
) -> list[DiscordMessage]:
    deduped = {item.id: item for item in channel_messages}
    deduped[message.id] = message
    return sorted(deduped.values(), key=lambda item: (item.timestamp, item.id))
