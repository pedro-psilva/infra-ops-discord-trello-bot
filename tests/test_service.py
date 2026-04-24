from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage, RequestedCard
from discord_trello_bot.service import DiscordTrelloService


def build_settings() -> Settings:
    return Settings(
        discord_bot_token="token",
        discord_guild_id="guild",
        discord_channel_ids=("channel",),
        discord_request_channel_ids=("request-channel",),
        discord_confirmation_mode=ConfirmationMode.REPLY,
        discord_reaction_emoji="\u2705",
        discord_reply_template="Card criado no Trello: {card_url}",
        trello_api_key="key",
        trello_api_token="token",
        trello_board_ref="https://trello.com/b/CCBHyCcR/operacoes-infra",
        trello_target_list_id="list",
        trello_target_list_name="Em andamento - Techs",
        trello_onboarding_template_card_ref="https://trello.com/c/gpsZOkXq/688-onboarding-envio-nome",
        trello_offboarding_template_card_ref="https://trello.com/c/lreoSMVL/437-offboarding-nome-data-ultimo-dia",
        trello_keep_from_source="checklists,customFields,labels",
        timezone=ZoneInfo("America/Sao_Paulo"),
        lookback_days=7,
        max_messages_per_channel=500,
        log_level="INFO",
    )


def build_message(
    *,
    message_id: str = "123",
    channel_id: str = "456",
    content: str = "Onboarding\nNome: Maria Silva\nData: 22/04/2026",
    author_is_bot: bool = False,
    referenced_message_id: str | None = None,
    mentioned_user_ids: tuple[str, ...] = (),
    mentioned_role_ids: tuple[str, ...] = (),
    hour: int = 10,
    minute: int = 0,
) -> DiscordMessage:
    return DiscordMessage(
        id=message_id,
        channel_id=channel_id,
        content=content,
        timestamp=datetime(2026, 4, 24, hour, minute, tzinfo=ZoneInfo("America/Sao_Paulo")),
        author_id="789",
        author_name="RH",
        author_is_bot=author_is_bot,
        message_type=0,
        webhook_id=None,
        referenced_channel_id=channel_id,
        referenced_message_id=referenced_message_id,
        mentioned_user_ids=mentioned_user_ids,
        mentioned_role_ids=mentioned_role_ids,
        reactions=(),
    )


class DiscordTrelloServiceTests(unittest.TestCase):
    def test_skip_when_message_already_has_bot_reply_confirmation(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.parser = Mock()
        service.trello = Mock()
        service.discord = Mock()

        message = build_message()
        outcome, created_count = service._process_message(
            message,
            channel_messages=[message],
            bot_reply_reference_ids={message.id},
            modes={"structured"},
            bot_user_id=None,
            bot_role_ids=set(),
        )

        self.assertEqual(outcome, "already_confirmed")
        self.assertEqual(created_count, 0)
        service.parser.parse_message.assert_not_called()
        service.trello.create_card_from_template.assert_not_called()
        service.discord.reply_to_message.assert_not_called()

    def test_create_card_when_bot_is_mentioned_in_request_channel(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.parser = Mock()
        service.request_parser = Mock()
        service.trello = Mock()
        service.discord = Mock()

        command_message = build_message(
            channel_id="request-channel",
            content="<@bot> crie um card sobre isso para segunda feira",
            referenced_message_id="source-1",
            mentioned_user_ids=("bot-1",),
        )
        context_message = build_message(
            channel_id="request-channel",
            content="Precisamos revisar o contrato do fornecedor X",
        )
        service.discord.get_message.return_value = context_message
        service.request_parser.parse.return_value = (
            RequestedCard(
                title="[Discord] Precisamos revisar o contrato do fornecedor X",
                due_date=datetime(2026, 4, 27).date(),
                instruction="crie um card sobre isso para segunda feira",
                source_excerpt=context_message.content,
                context_excerpt=f"{context_message.author_name}: {context_message.content}",
            ),
            [context_message],
            None,
        )
        service.trello.create_card.return_value = {"id": "card-1", "url": "https://trello/card-1"}

        outcome, created_count = service._process_message(
            command_message,
            channel_messages=[context_message, command_message],
            bot_reply_reference_ids=set(),
            modes={"request"},
            bot_user_id="bot-1",
            bot_role_ids=set(),
        )

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 1)
        service.request_parser.parse.assert_called_once()
        service.trello.create_card.assert_called_once()
        service.trello.add_comment.assert_called_once()
        service.discord.add_reaction.assert_called_once()
        service.discord.reply_to_message.assert_called_once()

    def test_recent_request_context_loads_messages_from_same_day(self) -> None:
        service = DiscordTrelloService(build_settings())
        command_message = build_message(
            message_id="cmd-1",
            channel_id="request-channel",
            content="<@bot> crie um card sobre isso",
            hour=11,
            minute=0,
        )
        older_message = build_message(
            message_id="old-1",
            channel_id="request-channel",
            content="Assunto antigo",
            hour=9,
            minute=40,
        )
        in_window_message = build_message(
            message_id="win-1",
            channel_id="request-channel",
            content="Assunto ainda recente",
            hour=10,
            minute=15,
        )

        messages = service._load_recent_request_messages(
            command_message=command_message,
            channel_messages=[older_message, in_window_message, command_message],
        )

        self.assertEqual([message.id for message in messages], ["old-1", "win-1"])

    def test_create_card_when_bot_role_is_mentioned_in_request_channel(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.parser = Mock()
        service.request_parser = Mock()
        service.trello = Mock()
        service.discord = Mock()

        command_message = build_message(
            channel_id="request-channel",
            content="<@&role-1> crie um card sobre isso para segunda feira",
            referenced_message_id="source-1",
            mentioned_role_ids=("role-1",),
        )
        context_message = build_message(
            channel_id="request-channel",
            content="Precisamos revisar o contrato do fornecedor X",
        )
        service.discord.get_message.return_value = context_message
        service.request_parser.parse.return_value = (
            RequestedCard(
                title="[Discord] Precisamos revisar o contrato do fornecedor X",
                due_date=datetime(2026, 4, 27).date(),
                instruction="crie um card sobre isso para segunda feira",
                source_excerpt=context_message.content,
                context_excerpt=f"{context_message.author_name}: {context_message.content}",
            ),
            [context_message],
            None,
        )
        service.trello.create_card.return_value = {"id": "card-1", "url": "https://trello/card-1"}

        outcome, created_count = service._process_message(
            command_message,
            channel_messages=[context_message, command_message],
            bot_reply_reference_ids=set(),
            modes={"request"},
            bot_user_id="bot-1",
            bot_role_ids={"role-1"},
        )

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 1)
        service.request_parser.parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
