from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage, EmailMessage, RequestedCard, TaskCard, TaskType
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
        service.trello.find_open_card_by_name.return_value = None

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
        service.request_refiner = Mock()
        service.trello = Mock()
        service.discord = Mock()
        service.trello.find_open_card_by_name.return_value = None

        command_message = build_message(
            channel_id="request-channel",
            content="<@bot> crie um card sobre isso para segunda feira",
            referenced_message_id="source-1",
            mentioned_user_ids=("bot-1",),
        )
        context_message = build_message(
            channel_id="request-channel",
            content="Precisamos revisar o contrato do fornecedor X: https://example.com/contrato?draft=1.",
        )
        service.discord.get_message.return_value = context_message
        service.request_parser.parse.return_value = (
            RequestedCard(
                title="Revisar contrato do fornecedor X",
                summary="Revisar o contrato do fornecedor X antes da renovacao.",
                due_date=datetime(2026, 4, 27).date(),
                instruction="crie um card sobre isso para segunda feira",
                source_excerpt=context_message.content,
                context_excerpt="O juridico pediu os ajustes no aditivo antes da renovacao.",
            ),
            [context_message],
            None,
        )
        service.request_refiner.refine.return_value = RequestedCard(
            title="Revisar contrato do fornecedor X com juridico",
            summary="Revisar o contrato do fornecedor X com apoio do juridico antes da renovacao.",
            due_date=datetime(2026, 4, 27).date(),
            instruction="crie um card sobre isso para segunda feira",
            source_excerpt=context_message.content,
            context_excerpt="Validar ajustes no aditivo antes de fechar a renovacao.",
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
        service.request_refiner.refine.assert_called_once()
        service.trello.create_card.assert_called_once()
        create_kwargs = service.trello.create_card.call_args.kwargs
        self.assertEqual(create_kwargs["card_name"], "Revisar contrato do fornecedor X com juridico")
        self.assertIn("**Resumo da solicitacao:**", create_kwargs["desc"])
        self.assertIn("Revisar o contrato do fornecedor X com apoio do juridico antes da renovacao.", create_kwargs["desc"])
        self.assertIn("**Solicitante:** RH", create_kwargs["desc"])
        self.assertIn("**Detalhes importantes:**", create_kwargs["desc"])
        self.assertIn("**Links citados:**", create_kwargs["desc"])
        self.assertIn("https://example.com/contrato?draft=1", create_kwargs["desc"])
        self.assertNotIn("Contexto de apoio", create_kwargs["desc"])
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
        service.request_refiner = Mock()
        service.trello = Mock()
        service.discord = Mock()
        service.trello.find_open_card_by_name.return_value = None

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
                title="Revisar contrato do fornecedor X",
                summary="Revisar o contrato do fornecedor X antes da renovacao.",
                due_date=datetime(2026, 4, 27).date(),
                instruction="crie um card sobre isso para segunda feira",
                source_excerpt=context_message.content,
                context_excerpt="O juridico pediu os ajustes no aditivo antes da renovacao.",
            ),
            [context_message],
            None,
        )
        service.request_refiner.refine.return_value = RequestedCard(
            title="Revisar contrato do fornecedor X",
            summary="Revisar o contrato do fornecedor X antes da renovacao.",
            due_date=datetime(2026, 4, 27).date(),
            instruction="crie um card sobre isso para segunda feira",
            source_excerpt=context_message.content,
            context_excerpt="O juridico pediu os ajustes no aditivo antes da renovacao.",
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

    def test_request_falls_back_to_heuristic_when_openai_refiner_fails(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.parser = Mock()
        service.request_parser = Mock()
        service.request_refiner = Mock()
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
        heuristic_card = RequestedCard(
            title="Revisar contrato do fornecedor X",
            summary="Revisar o contrato do fornecedor X antes da renovacao.",
            due_date=datetime(2026, 4, 27).date(),
            instruction="crie um card sobre isso para segunda feira",
            source_excerpt=context_message.content,
            context_excerpt="O juridico pediu os ajustes no aditivo antes da renovacao.",
        )
        service.discord.get_message.return_value = context_message
        service.request_parser.parse.return_value = (heuristic_card, [context_message], None)
        service.request_refiner.refine.side_effect = ValueError("falha simulada")
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
        create_kwargs = service.trello.create_card.call_args.kwargs
        self.assertEqual(create_kwargs["card_name"], "Revisar contrato do fornecedor X")

    def test_create_onboarding_card_from_email_with_address_notes(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.find_open_card_by_name.return_value = None
        service.trello.create_card_from_template.return_value = {
            "id": "card-1",
            "url": "https://trello/card-1",
        }
        email_message = EmailMessage(
            id="email-1",
            thread_id="thread-1",
            sender="rh@example.com",
            subject="Onboarding Ana Paula Souza",
            body=(
                "Nome Completo: Ana Paula Souza\n"
                "Data de Admissao: 05/05/2026\n"
                "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP\n"
                "CEP: 01000-000\n"
                "Telefone: (11) 99999-9999"
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 1)
        service.trello.create_card_from_template.assert_called_once()
        service.trello.add_comment.assert_called_once()
        comment = service.trello.add_comment.call_args.kwargs["text"]
        self.assertIn("Origem no e-mail:", comment)
        self.assertIn("Informacoes adicionais detectadas:", comment)
        self.assertIn("Endereco: Rua das Flores", comment)
        self.assertIn("CEP: 01000-000", comment)
        service.gmail.mark_processed.assert_called_once_with("email-1")

    def test_email_adds_comment_to_existing_onboarding_card_instead_of_creating_duplicate(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.find_open_card_by_name.return_value = {
            "id": "existing-card",
            "url": "https://trello/existing-card",
            "name": "[Onboarding] Ana Paula Souza - 05/05/2026",
        }
        email_message = EmailMessage(
            id="email-2",
            thread_id="thread-2",
            sender="rh@example.com",
            subject="Onboarding Ana Paula Souza",
            body=(
                "Nome Completo: Ana Paula Souza\n"
                "Data de Admissao: 05/05/2026\n"
                "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP\n"
                "Cargo: Analista de Operacoes"
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_called_once()
        self.assertEqual(service.trello.add_comment.call_args.kwargs["card_id"], "existing-card")
        comment = service.trello.add_comment.call_args.kwargs["text"]
        self.assertIn("Endereco: Rua das Flores", comment)
        self.assertIn("Cargo: Analista de Operacoes", comment)

    def test_create_offboarding_card_from_email_with_today_and_recipient_name(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.find_open_card_by_name.return_value = None
        service.trello.create_card_from_template.return_value = {
            "id": "card-offboarding",
            "url": "https://trello/card-offboarding",
        }
        email_message = EmailMessage(
            id="email-offboarding-1",
            thread_id="thread-offboarding-1",
            sender="rh@example.com",
            recipient="Jucilene Silva <jucilene.silva@example.com>",
            subject="[Infra] Orientacoes ao processo de Offboarding",
            body=(
                "Boa tarde, Jucilene! Como voce esta? Espero que se encontre bem!\n\n"
                "Venho trazer mais informacoes sobre os proximos passos no processo de desligamento, "
                "visto que seu ultimo dia trabalhado sera hoje."
            ),
            timestamp=datetime(2026, 5, 5, 15, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 1)
        service.trello.create_card_from_template.assert_called_once()
        create_kwargs = service.trello.create_card_from_template.call_args.kwargs
        self.assertEqual(create_kwargs["task_type"], TaskType.OFFBOARDING)
        self.assertEqual(create_kwargs["card_name"], "[Offboarding] Jucilene Silva - 05/05/2026")
        service.trello.add_comment.assert_called_once()
        comment = service.trello.add_comment.call_args.kwargs["text"]
        self.assertIn("Origem no e-mail:", comment)
        self.assertIn("Destinatario: Jucilene Silva <jucilene.silva@example.com>", comment)
        self.assertIn("Tipo: Offboarding", comment)
        self.assertIn("Data: 05/05/2026", comment)
        self.assertIn("ultimo dia trabalhado sera hoje", comment)
        service.gmail.mark_processed.assert_called_once_with("email-offboarding-1")

    def test_discord_complement_comments_future_onboarding_card(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.discord = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="card-ana",
                url="https://trello/card-ana",
                name="[Onboarding] Ana Paula Souza - 04/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=datetime(2026, 5, 4).date(),
            )
        ]
        message = build_message(
            content="Ana do dia 04/05 nao vai precisar de notebook.",
            hour=11,
            minute=30,
        )

        outcome, created_count = service._process_message(
            message,
            channel_messages=[message],
            bot_reply_reference_ids=set(),
            modes={"structured"},
            bot_user_id=None,
            bot_role_ids=set(),
        )

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_called_once()
        self.assertEqual(service.trello.add_comment.call_args.kwargs["card_id"], "card-ana")
        comment = service.trello.add_comment.call_args.kwargs["text"]
        self.assertIn("Complemento detectado no Discord:", comment)
        self.assertIn("Ana do dia 04/05 nao vai precisar de notebook.", comment)
        service.discord.add_reaction.assert_called_once()

    def test_discord_complement_ignores_past_onboarding_card(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.discord = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="card-old",
                url="https://trello/card-old",
                name="[Onboarding] Ana Paula Souza - 20/04/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=datetime(2026, 4, 20).date(),
            )
        ]
        message = build_message(
            content="Ana Paula Souza nao vai precisar de notebook.",
            hour=11,
            minute=30,
        )

        outcome, created_count = service._process_message(
            message,
            channel_messages=[message],
            bot_reply_reference_ids=set(),
            modes={"structured"},
            bot_user_id=None,
            bot_role_ids=set(),
        )

        self.assertEqual(outcome, "skipped")
        self.assertEqual(created_count, 0)
        service.trello.add_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
