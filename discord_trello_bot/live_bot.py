from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord

from .config import Settings
from .models import DiscordMessage, DiscordReaction
from .service import DiscordTrelloService


LOGGER = logging.getLogger(__name__)


class InfraOpsDiscordClient(discord.Client):
    def __init__(self, *, settings: Settings, service: DiscordTrelloService) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.service = service
        self._bot_user_id: str | None = None
        self._bot_role_ids: set[str] | None = None

    async def on_ready(self) -> None:
        if self.user is not None:
            self._bot_user_id = str(self.user.id)
        LOGGER.info("Infra Ops conectado ao Discord como %s.", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        channel_id = str(message.channel.id)
        modes = self._channel_modes(channel_id)
        if not modes:
            return

        incoming_message = _message_from_discord_py(message)
        if "request" in modes and not self._mentions_bot_or_role(incoming_message):
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
