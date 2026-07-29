from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from discord_trello_bot.dm_conversation import DMCardDraft, DMDecision
from discord_trello_bot.live_bot import InfraOpsDiscordClient
from discord_trello_bot.service import DiscordTrelloService
from tests.test_service import build_settings


BOT_USER_ID = "botid"
BOT_ROLE_ID = "botrole"


class _FakeAuthor:
    def __init__(self, *, user_id: str, bot: bool = False, name: str = "Fulano") -> None:
        self.id = user_id
        self.bot = bot
        self.global_name = name
        self.display_name = name
        self.name = name


class _FakeRole:
    def __init__(self, role_id: str) -> None:
        self.id = role_id


class _FakeMember:
    def __init__(self, role_ids: list[str]) -> None:
        self.roles = [_FakeRole(rid) for rid in role_ids]


class _FakeGuild:
    def __init__(self, guild_id: str, bot_role_ids: list[str]) -> None:
        self.id = guild_id
        self.me = _FakeMember(bot_role_ids)


class _FakeChannel:
    def __init__(self, channel_id: str) -> None:
        self.id = channel_id


class _FakeType:
    def __init__(self, value: int = 0) -> None:
        self.value = value


class _FakeMessage:
    def __init__(
        self,
        *,
        content: str,
        channel_id: str,
        guild_id: str | None = "guild",
        author_bot: bool = False,
        mentions_bot: bool = False,
        mentions_role: bool = False,
        message_id: str = "m1",
    ) -> None:
        self.id = message_id
        self.content = content
        self.channel = _FakeChannel(channel_id)
        self.guild = (
            _FakeGuild(guild_id, [BOT_ROLE_ID]) if guild_id is not None else None
        )
        self.author = _FakeAuthor(user_id="789", bot=author_bot)
        self.created_at = datetime(2026, 7, 29, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
        self.type = _FakeType(0)
        self.webhook_id = None
        self.reference = None
        self.mentions = [_FakeAuthor(user_id=BOT_USER_ID, bot=True)] if mentions_bot else []
        self.role_mentions = [_FakeRole(BOT_ROLE_ID)] if mentions_role else []
        self.reactions = []


def _build_client(chat_ids: tuple[str, ...]):
    settings = replace(build_settings(), discord_chat_channel_ids=chat_ids)
    service = DiscordTrelloService(settings)
    service.discord = Mock()
    service.discord.get_message.return_value = None
    service.discord.iter_messages_since.return_value = []
    service.trello = Mock()
    service.trello.find_open_card_by_name.return_value = None
    service.chat_assistant = Mock()
    client = InfraOpsDiscordClient(settings=settings, service=service)
    client._bot_user_id = BOT_USER_ID
    return client, service


class LiveBotChatRoutingTests(unittest.TestCase):
    def test_mention_in_chat_channel_triggers_conversation_reply(self) -> None:
        client, service = _build_client(("chat-1",))
        service.chat_assistant.decide.return_value = DMDecision(
            action="ask", reply="Claro, posso ajudar!", card=None
        )
        message = _FakeMessage(
            content="<@botid> tudo bem?", channel_id="chat-1", mentions_bot=True
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_called_once()
        service.trello.create_card.assert_not_called()
        self.assertEqual(
            service.discord.reply_to_message.call_args.kwargs["content"], "Claro, posso ajudar!"
        )

    def test_mention_creates_card(self) -> None:
        from datetime import date

        client, service = _build_client(("chat-1",))
        service.trello.create_card.return_value = {
            "id": "c1",
            "url": "https://trello.com/c/AAA/1-nb",
        }
        service.chat_assistant.decide.return_value = DMDecision(
            action="create",
            reply="Feito!",
            card=DMCardDraft(
                title="Configurar notebook do Joao",
                description="Preparar maquina",
                due_date=date(2026, 8, 1),
            ),
        )
        message = _FakeMessage(
            content="<@botid> pode criar", channel_id="chat-1", mentions_bot=True
        )

        asyncio.run(client.on_message(message))

        service.trello.create_card.assert_called_once()
        self.assertIn(
            "https://trello.com/c/AAA/1-nb",
            service.discord.reply_to_message.call_args.kwargs["content"],
        )

    def test_role_mention_in_chat_channel_triggers_conversation(self) -> None:
        client, service = _build_client(("chat-1",))
        service.chat_assistant.decide.return_value = DMDecision(
            action="ask", reply="Oi!", card=None
        )
        message = _FakeMessage(
            content="<@&botrole> ajuda", channel_id="chat-1", mentions_role=True
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_called_once()

    def test_guild_id_in_chat_set_matches_any_channel(self) -> None:
        client, service = _build_client(("guild",))
        service.chat_assistant.decide.return_value = DMDecision(
            action="ask", reply="Oi!", card=None
        )
        message = _FakeMessage(
            content="<@botid> oi", channel_id="qualquer-canal", mentions_bot=True
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_called_once()

    def test_no_mention_in_pure_chat_channel_stays_quiet(self) -> None:
        client, service = _build_client(("chat-1",))
        message = _FakeMessage(
            content="conversa entre humanos", channel_id="chat-1", mentions_bot=False
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_not_called()
        service.discord.reply_to_message.assert_not_called()

    def test_no_mention_in_structured_channel_uses_existing_flow(self) -> None:
        client, service = _build_client(("channel",))
        client._process_live_message = Mock(return_value=("skipped", 0))
        message = _FakeMessage(
            content="Onboarding\nNome: Maria Silva\nData: 22/04/2026",
            channel_id="channel",
            mentions_bot=False,
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_not_called()
        client._process_live_message.assert_called_once()

    def test_mention_in_structured_and_chat_channel_prefers_chat(self) -> None:
        client, service = _build_client(("channel",))
        client._process_live_message = Mock(return_value=("skipped", 0))
        service.chat_assistant.decide.return_value = DMDecision(
            action="ask", reply="Oi!", card=None
        )
        message = _FakeMessage(
            content="<@botid> e ai", channel_id="channel", mentions_bot=True
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_called_once()
        client._process_live_message.assert_not_called()

    def test_bot_author_is_ignored(self) -> None:
        client, service = _build_client(("chat-1",))
        message = _FakeMessage(
            content="<@botid> oi", channel_id="chat-1", mentions_bot=True, author_bot=True
        )

        asyncio.run(client.on_message(message))

        service.chat_assistant.decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
