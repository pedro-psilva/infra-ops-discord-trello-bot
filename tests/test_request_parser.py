from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage
from discord_trello_bot.request_parser import RequestParser


TIMEZONE = ZoneInfo("America/Sao_Paulo")


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
        timezone=TIMEZONE,
        lookback_days=7,
        max_messages_per_channel=500,
        log_level="INFO",
    )


def build_message(
    *,
    message_id: str,
    content: str,
    hour: int,
    minute: int = 0,
    author_id: str = "author-1",
    author_name: str = "Pedro Paulo",
    referenced_message_id: str | None = None,
    mentioned_user_ids: tuple[str, ...] = ("bot-1",),
    mentioned_role_ids: tuple[str, ...] = (),
) -> DiscordMessage:
    return DiscordMessage(
        id=message_id,
        channel_id="request-channel",
        content=content,
        timestamp=datetime(2026, 4, 24, hour, minute, tzinfo=TIMEZONE),
        author_id=author_id,
        author_name=author_name,
        author_is_bot=False,
        message_type=0,
        webhook_id=None,
        referenced_channel_id="request-channel",
        referenced_message_id=referenced_message_id,
        mentioned_user_ids=mentioned_user_ids,
        mentioned_role_ids=mentioned_role_ids,
        reactions=(),
    )


class RequestParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = RequestParser(build_settings())

    def test_parse_request_uses_topic_slice_from_same_day_conversation(self) -> None:
        command_message = build_message(
            message_id="cmd-1",
            content="<@bot-1>, crie um card sobre isso para segunda feira",
            hour=11,
            minute=35,
            author_name="Pedro Paulo",
            referenced_message_id="ctx-4",
        )
        context_messages = [
            build_message(
                message_id="ctx-1",
                content="Bom dia",
                hour=9,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-2",
                content="Precisamos revisar o contrato do fornecedor X antes da renovacao.",
                hour=10,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-3",
                content="O juridico pediu os ajustes no aditivo e a planilha de custos.",
                hour=10,
                minute=5,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-4",
                content="Se nao fecharmos hoje, a renovacao vira automatica.",
                hour=10,
                minute=10,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-5",
                content="Alguem viu o monitor da recepcao?",
                hour=11,
                minute=30,
                author_id="author-3",
                author_name="Carlos Lima",
                mentioned_user_ids=(),
            ),
        ]

        requested_card, selected_context_messages, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            recent_channel_messages=context_messages,
            reply_chain_messages=[context_messages[3]],
        )

        self.assertIsNone(reason)
        assert requested_card is not None
        self.assertEqual(
            requested_card.title,
            "[Discord] Precisamos revisar o contrato do fornecedor X antes da renovacao.",
        )
        self.assertEqual(requested_card.due_date.isoformat(), "2026-04-27")
        self.assertEqual([message.id for message in selected_context_messages], ["ctx-2", "ctx-3", "ctx-4"])
        self.assertIn("Bianca Oliveira: Precisamos revisar o contrato do fornecedor X", requested_card.context_excerpt)
        self.assertIn("Pedro Paulo: O juridico pediu os ajustes no aditivo", requested_card.context_excerpt)
        self.assertNotIn("Bom dia", requested_card.context_excerpt)
        self.assertNotIn("monitor da recepcao", requested_card.context_excerpt)

    def test_parse_request_without_reply_uses_recent_same_topic_messages(self) -> None:
        command_message = build_message(
            message_id="cmd-2",
            content="<@bot-1> crie um card sobre isso para amanha",
            hour=10,
            minute=20,
            referenced_message_id=None,
        )
        recent_messages = [
            build_message(
                message_id="ctx-10",
                content="Bom dia",
                hour=8,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-11",
                content="O notebook da Julia esta sem acesso a VPN.",
                hour=10,
                minute=5,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-12",
                content="Aparece erro 691 no acesso remoto dela.",
                hour=10,
                minute=8,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
        ]

        requested_card, selected_context_messages, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            recent_channel_messages=recent_messages,
            reply_chain_messages=[],
        )

        self.assertIsNone(reason)
        assert requested_card is not None
        self.assertEqual([message.id for message in selected_context_messages], ["ctx-11", "ctx-12"])
        self.assertIn("VPN", requested_card.context_excerpt)
        self.assertNotIn("Bom dia", requested_card.context_excerpt)

    def test_parse_request_can_use_full_last_hour_when_topic_continues(self) -> None:
        command_message = build_message(
            message_id="cmd-4",
            content="<@bot-1> crie um card sobre isso para quarta",
            hour=11,
            minute=0,
            referenced_message_id="ctx-35",
        )
        recent_messages = [
            build_message(
                message_id="ctx-30",
                content="Chamado da VPN da Julia segue aberto.",
                hour=10,
                minute=2,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-31",
                content="Ela ainda nao consegue autenticar no notebook novo.",
                hour=10,
                minute=10,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-32",
                content="O erro continua mesmo apos resetar a senha.",
                hour=10,
                minute=18,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-33",
                content="Tambem precisamos validar o cliente da VPN no equipamento.",
                hour=10,
                minute=29,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-34",
                content="Vou pegar os logs e anexar no atendimento.",
                hour=10,
                minute=41,
                author_id="author-2",
                author_name="Bianca Oliveira",
                mentioned_user_ids=(),
            ),
            build_message(
                message_id="ctx-35",
                content="Se nao normalizar hoje, precisamos abrir tratativa com o fornecedor.",
                hour=10,
                minute=52,
                author_name="Pedro Paulo",
                mentioned_user_ids=(),
            ),
        ]

        requested_card, selected_context_messages, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            recent_channel_messages=recent_messages,
            reply_chain_messages=[recent_messages[-1]],
        )

        self.assertIsNone(reason)
        assert requested_card is not None
        self.assertEqual(
            [message.id for message in selected_context_messages],
            ["ctx-30", "ctx-31", "ctx-32", "ctx-33", "ctx-34", "ctx-35"],
        )
        self.assertIn("tratativa com o fornecedor", requested_card.context_excerpt)
        self.assertIn("cliente da VPN", requested_card.context_excerpt)

    def test_skip_when_mention_is_not_a_card_request(self) -> None:
        command_message = build_message(
            message_id="cmd-3",
            content="<@bot-1>, olha isso depois",
            hour=10,
        )
        context_messages = [build_message(message_id="ctx-20", content="Mensagem de contexto", hour=9)]

        requested_card, selected_context_messages, reason = self.parser.parse(
            command_message=command_message,
            bot_user_id="bot-1",
            recent_channel_messages=context_messages,
            reply_chain_messages=[],
        )

        self.assertIsNone(requested_card)
        self.assertEqual(selected_context_messages, [])
        self.assertEqual(reason, "mencao sem pedido de card")


if __name__ == "__main__":
    unittest.main()
