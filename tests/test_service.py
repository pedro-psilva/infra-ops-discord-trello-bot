from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage
from discord_trello_bot.service import DiscordTrelloService


def build_settings() -> Settings:
    return Settings(
        discord_bot_token="token",
        discord_guild_id="guild",
        discord_channel_ids=("channel",),
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


def build_message(*, author_is_bot: bool = False, referenced_message_id: str | None = None) -> DiscordMessage:
    return DiscordMessage(
        id="123",
        channel_id="456",
        content="Onboarding\nNome: Maria Silva\nData: 22/04/2026",
        timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
        author_id="789",
        author_name="RH",
        author_is_bot=author_is_bot,
        message_type=0,
        webhook_id=None,
        referenced_message_id=referenced_message_id,
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
            bot_reply_reference_ids={message.id},
        )

        self.assertEqual(outcome, "already_confirmed")
        self.assertEqual(created_count, 0)
        service.parser.parse_message.assert_not_called()
        service.trello.create_card_from_template.assert_not_called()
        service.discord.reply_to_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
