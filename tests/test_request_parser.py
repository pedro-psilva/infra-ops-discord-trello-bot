from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage
from discord_trello_bot.request_parser import RequestParser


def build_settings() -> Settings:
    return Settings(
        discord_bot_token="token",
        discord_guild_id="guild",
        discord_channel_ids=("channel",),
        discord_request_channel_ids=("request-channel",),
        discord_confirmation_mode=ConfirmationMode.REACTION,
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


def build_message(*, message_id: str, content: str, author_name: str = "Pedro Paulo") -> DiscordMessage:
    return DiscordMessage(
        id=message_id,
        channel_id="request-channel",
        content=content,
        timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
        author_id="author-1",
        author_name=author_name,
        author_is_bot=False,
        message_type=0,
        webhook_id=None,
        referenced_channel_id="request-channel",
        referenced_message_id=None,
        mentioned_user_ids=("bot-1",),
        reactions=(),
    )


class RequestParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = RequestParser(build_settings())

    def test_parse_request_uses_replied_message_as_title(self) -> None:
        command_message = build_message(
            message_id="cmd-1",
            content="<@bot-1>, crie um card sobre isso para segunda feira",
        )
        context_messages = [
            build_message(
                message_id="ctx-1",
                content="Precisamos revisar o contrato do fornecedor X antes da renovacao.",
                author_name="Bianca Oliveira",
            )
        ]

        requested_card, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            context_messages=context_messages,
        )

        self.assertIsNone(reason)
        assert requested_card is not None
        self.assertEqual(
            requested_card.title,
            "[Discord] Precisamos revisar o contrato do fornecedor X antes da renovacao.",
        )
        self.assertEqual(requested_card.due_date.isoformat(), "2026-04-27")

    def test_skip_when_mention_is_not_a_card_request(self) -> None:
        command_message = build_message(
            message_id="cmd-2",
            content="<@bot-1>, olha isso depois",
        )
        context_messages = [build_message(message_id="ctx-2", content="Mensagem de contexto")]

        requested_card, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            context_messages=context_messages,
        )

        self.assertIsNone(requested_card)
        self.assertEqual(reason, "mencao sem pedido de card")


if __name__ == "__main__":
    unittest.main()
