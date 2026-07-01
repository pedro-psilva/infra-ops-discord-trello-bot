from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage, EmailMessage, ParsedTask, RequestedCard, TaskCard, TaskType
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
        service.trello.list_open_task_cards.return_value = []
        service.trello.create_card_from_template.return_value = {
            "id": "card-1",
            "url": "https://trello/card-1",
        }
        service.trello.get_card.return_value = {"id": "card-1", "desc": "Checklist padrao do template"}
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
        self.assertIn("Informacoes adicionais adicionadas na descricao do card.", comment)
        self.assertNotIn("Card criado:", comment)
        self.assertNotIn("Conteudo original:", comment)
        service.trello.update_card_description.assert_called_once()
        desc = service.trello.update_card_description.call_args.kwargs["desc"]
        self.assertIn("## Informacoes de onboarding recebidas por e-mail", desc)
        self.assertIn("Endereco: Rua das Flores", desc)
        self.assertIn("CEP: 01000-000", desc)
        self.assertIn("Checklist padrao do template", desc)
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
        service.trello.get_card.return_value = {"id": "existing-card", "desc": ""}
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
        self.assertIn("Informacoes adicionais adicionadas na descricao do card.", comment)
        self.assertNotIn("Card criado:", comment)
        service.trello.update_card_description.assert_called_once()
        desc = service.trello.update_card_description.call_args.kwargs["desc"]
        self.assertIn("Endereco: Rua das Flores", desc)
        self.assertIn("Cargo: Analista de Operacoes", desc)

    def test_email_matches_existing_card_with_compatible_name_same_date(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.find_open_card_by_name.return_value = None
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="existing-card",
                url="https://trello/existing-card",
                name="[Onboarding] Ana Júlia Simões Silvério - 04/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Júlia Simões Silvério",
                effective_date=datetime(2026, 5, 4).date(),
            )
        ]
        service.trello.get_card.return_value = {"id": "existing-card", "desc": ""}
        email_message = EmailMessage(
            id="email-compatible-name",
            thread_id="thread-compatible-name",
            sender="rh@example.com",
            subject="Onboarding Ana Júlia!",
            body=(
                "Data de Admissao: 04/05/2026\n"
                "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP"
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        self.assertEqual(service.trello.add_comment.call_args.kwargs["card_id"], "existing-card")
        service.trello.update_card_description.assert_called_once()

    def test_email_with_only_details_updates_existing_future_onboarding_card(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="existing-card",
                url="https://trello/existing-card",
                name="[Onboarding] Ana Paula Souza - 05/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=datetime(2026, 5, 5).date(),
            )
        ]
        service.trello.get_card.return_value = {"id": "existing-card", "desc": ""}
        email_message = EmailMessage(
            id="email-details-only",
            thread_id="thread-details-only",
            sender="rh@example.com",
            subject="Dados cadastrais Ana Paula Souza",
            body=(
                "Ana Paula Souza\n"
                "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP\n"
                "CEP: 01000-000\n"
                "Cargo: Analista de Operacoes"
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "created")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_not_called()
        service.trello.update_card_description.assert_called_once()
        desc = service.trello.update_card_description.call_args.kwargs["desc"]
        self.assertIn("Endereco: Rua das Flores", desc)
        self.assertIn("Cargo: Analista de Operacoes", desc)
        service.gmail.mark_processed.assert_called_once_with("email-details-only")

    def test_onboarding_shipping_reply_email_does_not_create_card_without_existing_match(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.settings = replace(service.settings, gmail_user_email="pedro.paulo@iebtinnovation.com")
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.list_open_task_cards.return_value = []
        email_message = EmailMessage(
            id="email-shipping-reply",
            thread_id="thread-shipping-reply",
            sender="Maria Eduarda Neves <mariaeduarda.cns25@gmail.com>",
            recipient="Pedro Paulo <pedro.paulo@iebtinnovation.com>",
            subject="Re: Informacoes sobre envios - IEBT",
            body=(
                "Pedro Paulo\n"
                "30/04/2027\n"
                "Kit onboarding com notebook e perifericos.\n"
                "Rua Professor Jose Vieira de Mendonca, 770\n"
                "CEP: 31310-260 - Belo Horizonte - MG/Brasil"
            ),
            timestamp=datetime(2026, 4, 30, 10, 18, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "skipped")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_not_called()
        service.trello.update_card_description.assert_not_called()
        # Com a correcao, replies sao sempre marcados como processados para evitar reprocessamento
        service.gmail.mark_processed.assert_called_once_with("email-shipping-reply")

    def test_onboarding_reply_email_does_not_create_card(self) -> None:
        """Reply de onboarding nao deve criar card mesmo que o parser detecte tarefa."""
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.list_open_task_cards.return_value = []
        email_message = EmailMessage(
            id="email-onboarding-reply",
            thread_id="thread-onboarding-reply",
            sender="rh@example.com",
            recipient="ti@example.com",
            subject="Re: Onboarding Carlos Henrique - 12/05",
            body=(
                "Confirmado! Carlos Henrique comeca dia 12/05/2026.\n"
                "Pode preparar o equipamento."
            ),
            timestamp=datetime(2026, 5, 4, 9, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "skipped")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_not_called()
        service.trello.update_card_description.assert_not_called()
        service.gmail.mark_processed.assert_called_once_with("email-onboarding-reply")

    def test_enc_forward_email_does_not_create_card(self) -> None:
        """Encaminhamento com prefixo 'Enc:' (Outlook PT-BR) nao deve criar card."""
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.list_open_task_cards.return_value = []
        email_message = EmailMessage(
            id="email-enc-forward",
            thread_id="thread-enc-forward",
            sender="gestor@example.com",
            recipient="ti@example.com",
            subject="Enc: Offboarding Fernanda Lima - 15/05",
            body="Segue para conhecimento. Fernanda Lima sai dia 15/05/2026.",
            timestamp=datetime(2026, 5, 4, 14, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=(),
        )

        outcome, created_count = service._process_email_message(email_message)

        self.assertEqual(outcome, "skipped")
        self.assertEqual(created_count, 0)
        service.trello.create_card_from_template.assert_not_called()
        service.gmail.mark_processed.assert_called_once_with("email-enc-forward")

    def test_past_onboarding_is_stale_without_grace_period(self) -> None:
        service = DiscordTrelloService(build_settings())
        yesterday = datetime.now(tz=ZoneInfo("America/Sao_Paulo")).date() - timedelta(days=1)
        task = ParsedTask(
            task_type=TaskType.ONBOARDING,
            employee_name="Ana Paula Souza",
            effective_date=yesterday,
            notes=(),
            raw_excerpt="",
        )

        self.assertTrue(service._is_stale_task(task))

    def test_backfill_processed_email_updates_onboarding_description_only(self) -> None:
        service = DiscordTrelloService(
            replace(build_settings(), gmail_backfill_onboarding_descriptions=True)
        )
        service.trello = Mock()
        service.trello.find_open_card_by_name.return_value = {
            "id": "existing-card",
            "url": "https://trello/existing-card",
            "name": "[Onboarding] Ana Paula Souza - 05/05/2026",
        }
        service.trello.get_card.return_value = {"id": "existing-card", "desc": ""}
        email_message = EmailMessage(
            id="email-backfill",
            thread_id="thread-backfill",
            sender="rh@example.com",
            subject="Onboarding Ana Paula Souza",
            body=(
                "Nome Completo: Ana Paula Souza\n"
                "Data de Admissao: 05/05/2026\n"
                "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP\n"
                "CEP: 01000-000"
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=("processed",),
        )

        updated_count = service._backfill_onboarding_description_from_email(email_message)

        self.assertEqual(updated_count, 1)
        service.trello.create_card_from_template.assert_not_called()
        service.trello.add_comment.assert_not_called()
        service.trello.update_card_description.assert_called_once()
        desc = service.trello.update_card_description.call_args.kwargs["desc"]
        self.assertIn("Endereco: Rua das Flores", desc)
        self.assertIn("CEP: 01000-000", desc)

    def test_backfill_ignores_generic_onboarding_instruction_email(self) -> None:
        service = DiscordTrelloService(
            replace(build_settings(), gmail_backfill_onboarding_descriptions=True)
        )
        service.trello = Mock()
        email_message = EmailMessage(
            id="email-generic",
            thread_id="thread-generic",
            sender="rh@example.com",
            subject="Onboarding Ana Paula Souza",
            body=(
                "Nome Completo: Ana Paula Souza\n"
                "Data de Admissao: 05/05/2026\n"
                "Acesse seu e-mail institucional.\n"
                "Recebimento de notebook, perifericos e kit onboarding."
            ),
            timestamp=datetime(2026, 4, 24, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
            label_ids=("processed",),
        )

        updated_count = service._backfill_onboarding_description_from_email(email_message)

        self.assertEqual(updated_count, 0)
        service.trello.find_open_card_by_name.assert_not_called()
        service.trello.update_card_description.assert_not_called()

    def test_create_offboarding_card_from_email_with_today_and_recipient_name(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.gmail = Mock()
        service.trello.find_open_card_by_name.return_value = None
        service.trello.list_open_task_cards.return_value = []
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

    def test_recent_offboarding_with_yesterday_date_is_created(self) -> None:
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.discord = Mock()
        service.trello.find_open_card_by_name.return_value = None
        service.trello.list_open_task_cards.return_value = []
        service.trello.create_card_from_template.return_value = {
            "id": "card-offboarding",
            "url": "https://trello/card-offboarding",
        }
        today = datetime.now(tz=ZoneInfo("America/Sao_Paulo")).date()
        yesterday = today - timedelta(days=1)
        message = build_message(
            content=f"Offboarding Jucilene Aparecida - {yesterday.strftime('%d/%m')}",
            hour=12,
            minute=25,
        )
        message = DiscordMessage(
            **{
                **message.__dict__,
                "timestamp": datetime.combine(
                    today,
                    datetime.min.time().replace(hour=12, minute=25),
                    tzinfo=ZoneInfo("America/Sao_Paulo"),
                ),
            }
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
        self.assertEqual(created_count, 1)
        create_kwargs = service.trello.create_card_from_template.call_args.kwargs
        self.assertEqual(create_kwargs["task_type"], TaskType.OFFBOARDING)
        self.assertEqual(
            create_kwargs["card_name"],
            f"[Offboarding] Jucilene Aparecida - {yesterday.strftime('%d/%m/%Y')}",
        )
        service.trello.add_comment.assert_called_once()
        service.discord.add_reaction.assert_called_once()

class ExtractOnboardingEmailDescriptionDetailsTests(unittest.TestCase):
    def test_extracts_address_field(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        body = "Endereco: Rua das Flores, 123 - Centro - SP"
        result = _extract_onboarding_email_description_details(body)
        self.assertTrue(any("Rua das Flores" in item for item in result))

    def test_extracts_multiple_fields(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        body = "Cargo: Analista\nTelefone: 11 99999-0000\nModalidade: Hibrido"
        result = _extract_onboarding_email_description_details(body)
        self.assertEqual(len(result), 3)
        self.assertTrue(any("Cargo" in item for item in result))
        self.assertTrue(any("Telefone" in item for item in result))
        self.assertTrue(any("Modalidade" in item for item in result))

    def test_extracts_street_address_line(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        body = "Rua Professor Jose Vieira de Mendonca, 770"
        result = _extract_onboarding_email_description_details(body)
        self.assertGreaterEqual(len(result), 1)
        self.assertIn("Rua Professor Jose Vieira de Mendonca, 770", result)

    def test_ignores_lines_without_known_fields(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        body = "Ola tudo bem?\nPrecisamos conversar sobre o processo."
        result = _extract_onboarding_email_description_details(body)
        self.assertEqual(result, [])

    def test_field_with_bullet_prefix(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        body = "- Cargo: Desenvolvedor\n* Gestor: Carla Mendes"
        result = _extract_onboarding_email_description_details(body)
        self.assertEqual(len(result), 2)

    def test_empty_body(self) -> None:
        from discord_trello_bot.service import _extract_onboarding_email_description_details
        result = _extract_onboarding_email_description_details("")
        self.assertEqual(result, [])


class FindCompatibleTaskCardTests(unittest.TestCase):
    def test_find_compatible_task_card_exact_name_same_date(self) -> None:
        from datetime import date as date_cls
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="card-1",
                url="https://trello/card-1",
                name="[Onboarding] Ana Paula Souza - 15/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=date_cls(2026, 5, 15),
            )
        ]
        service.trello.find_open_card_by_name.return_value = None
        task = ParsedTask(
            task_type=TaskType.ONBOARDING,
            employee_name="Ana Paula Souza",
            effective_date=date_cls(2026, 5, 15),
            notes=(),
            raw_excerpt="",
        )
        result = service._find_compatible_task_card(task)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "card-1")

    def test_find_compatible_task_card_partial_name_same_date(self) -> None:
        from datetime import date as date_cls
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="card-2",
                url="https://trello/card-2",
                name="[Onboarding] Ana Paula Souza - 15/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=date_cls(2026, 5, 15),
            )
        ]
        service.trello.find_open_card_by_name.return_value = None
        task = ParsedTask(
            task_type=TaskType.ONBOARDING,
            employee_name="Ana Souza",
            effective_date=date_cls(2026, 5, 15),
            notes=(),
            raw_excerpt="",
        )
        result = service._find_compatible_task_card(task)
        self.assertIsNotNone(result)

    def test_find_compatible_task_card_no_match_different_date(self) -> None:
        from datetime import date as date_cls
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(
                id="card-3",
                url="https://trello/card-3",
                name="[Onboarding] Ana Paula Souza - 20/05/2026",
                task_type=TaskType.ONBOARDING,
                employee_name="Ana Paula Souza",
                effective_date=date_cls(2026, 5, 20),
            )
        ]
        service.trello.find_open_card_by_name.return_value = None
        task = ParsedTask(
            task_type=TaskType.ONBOARDING,
            employee_name="Ana Paula Souza",
            effective_date=date_cls(2026, 5, 15),
            notes=(),
            raw_excerpt="",
        )
        result = service._find_compatible_task_card(task)
        self.assertIsNone(result)


class CargoReplyTests(unittest.TestCase):
    def _build_service(self):
        from datetime import date as date_cls
        service = DiscordTrelloService(build_settings())
        service.trello = Mock()
        service.discord = Mock()
        service.trello.list_open_task_cards.return_value = [
            TaskCard(id="card-iza", url="https://trello/iza",
                name="[Onboarding] Izabela Linke de Avellar - 06/07/2026",
                task_type=TaskType.ONBOARDING, employee_name="Izabela Linke de Avellar",
                effective_date=date_cls(2026, 7, 6))]
        service.trello.get_card.return_value = {"id": "card-iza", "desc": ""}
        return service

    def test_cargo_reply_updates_card_description(self):
        from discord_trello_bot.models import RunSummary
        service = self._build_service()
        bot_q = build_message(message_id="bot1", author_is_bot=True,
            content="Card criado para **Izabela Linke de Avellar** em **06/07/2026** ✅\nNão identifiquei o cargo desta pessoa. Qual é o cargo? [cargo?]")
        human = build_message(message_id="h1", content="Business Analyst", referenced_message_id="bot1")
        processed = service._process_cargo_reply_messages(messages=[bot_q, human], channel_id="channel", summary=RunSummary())
        self.assertIn("h1", processed)
        service.trello.update_card_description.assert_called_once_with(card_id="card-iza", desc="**Cargo:** Business Analyst")
        service.discord.add_reaction.assert_called_once()

    def test_cargo_reply_matches_fallback_question_format(self):
        from discord_trello_bot.models import RunSummary
        service = self._build_service()
        fb = build_message(message_id="bot2", author_is_bot=True,
            content="Onboarding de **Izabela Linke de Avellar** em **06/07/2026** foi registrado, mas o cargo nao foi identificado. [cargo?]")
        human = build_message(message_id="h2", content="Business Analyst", referenced_message_id="bot2")
        service._process_cargo_reply_messages(messages=[fb, human], channel_id="channel", summary=RunSummary())
        service.trello.update_card_description.assert_called_once_with(card_id="card-iza", desc="**Cargo:** Business Analyst")


class OpenAIRequestRefinerTests(unittest.TestCase):
    def _build_refiner_settings(self) -> Settings:
        return replace(build_settings(), openai_api_key="sk-test-key")

    def test_refine_returns_improved_card(self) -> None:
        from discord_trello_bot.openai_request_refiner import OpenAIRequestRefiner
        from datetime import date
        settings = self._build_refiner_settings()
        refiner = OpenAIRequestRefiner(settings)
        refiner.client = Mock()
        refiner.client.request.return_value = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": '{"title": "Revisar contrato", "summary": "Revisar o contrato antes do prazo.", "details": "Validar com o juridico."}',
                }],
            }]
        }
        card = RequestedCard(
            title="revisar contrato",
            summary="revisar",
            due_date=date(2026, 5, 10),
            instruction="crie card",
            source_excerpt="revisar contrato",
            context_excerpt="",
        )
        result = refiner.refine(requested_card=card, command_message=build_message(), context_messages=[])
        self.assertEqual(result.title, "Revisar contrato")
        self.assertIn("prazo", result.summary)

    def test_refine_raises_on_empty_response(self) -> None:
        from discord_trello_bot.openai_request_refiner import OpenAIRequestRefiner
        settings = self._build_refiner_settings()
        refiner = OpenAIRequestRefiner(settings)
        refiner.client = Mock()
        refiner.client.request.return_value = {"output": []}
        card = RequestedCard(title="x", summary="y", due_date=None, instruction="z", source_excerpt="", context_excerpt="")
        with self.assertRaises(ValueError):
            refiner.refine(requested_card=card, command_message=build_message(), context_messages=[])

    def test_refine_raises_on_invalid_json(self) -> None:
        from discord_trello_bot.openai_request_refiner import OpenAIRequestRefiner
        settings = self._build_refiner_settings()
        refiner = OpenAIRequestRefiner(settings)
        refiner.client = Mock()
        refiner.client.request.return_value = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "nao e json"}]}]
        }
        card = RequestedCard(title="x", summary="y", due_date=None, instruction="z", source_excerpt="", context_excerpt="")
        with self.assertRaises(ValueError):
            refiner.refine(requested_card=card, command_message=build_message(), context_messages=[])

    def test_refine_raises_when_title_missing(self) -> None:
        from discord_trello_bot.openai_request_refiner import OpenAIRequestRefiner
        settings = self._build_refiner_settings()
        refiner = OpenAIRequestRefiner(settings)
        refiner.client = Mock()
        refiner.client.request.return_value = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"title": "", "summary": "algo", "details": ""}'}]}]
        }
        card = RequestedCard(title="x", summary="y", due_date=None, instruction="z", source_excerpt="", context_excerpt="")
        with self.assertRaises(ValueError):
            refiner.refine(requested_card=card, command_message=build_message(), context_messages=[])


if __name__ == "__main__":
    unittest.main()
