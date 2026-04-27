from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from discord_trello_bot.config import ConfirmationMode, Settings
from discord_trello_bot.models import DiscordMessage, TaskType
from discord_trello_bot.parser import TaskParser


def build_settings() -> Settings:
    return Settings(
        discord_bot_token="token",
        discord_guild_id="guild",
        discord_channel_ids=("channel",),
        discord_request_channel_ids=(),
        discord_confirmation_mode=ConfirmationMode.REACTION,
        discord_reaction_emoji="✅",
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


def build_message(content: str) -> DiscordMessage:
    return DiscordMessage(
        id="1",
        channel_id="2",
        content=content,
        timestamp=datetime(2026, 4, 17, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
        author_id="3",
        author_name="RH",
        author_is_bot=False,
        message_type=0,
        webhook_id=None,
        referenced_channel_id=None,
        referenced_message_id=None,
        mentioned_user_ids=(),
        mentioned_role_ids=(),
        reactions=(),
    )


class TaskParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = TaskParser(build_settings())

    def test_parse_structured_onboarding(self) -> None:
        message = build_message(
            "Onboarding\nNome: Maria Silva\nData: 22/04/2026\nObs: quer monitor e teclado, vai buscar no escritorio."
        )
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.task_type, TaskType.ONBOARDING)
        self.assertEqual(result.task.employee_name, "Maria Silva")
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-22")
        self.assertTrue(any("monitor" in note.lower() for note in result.task.notes))

    def test_parse_structured_onboarding_with_nome_completo(self) -> None:
        message = build_message(
            "Onboarding:\n\nNome Completo: Simone Tolisano Meirelles\nData de Admissao: 27/04/2026\nNotebook e Perifericos: Apenas Notebook"
        )
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Simone Tolisano Meirelles")
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-27")

    def test_parse_onboarding_keeps_address_as_note(self) -> None:
        message = build_message(
            "Onboarding\n"
            "Nome Completo: Ana Paula Souza\n"
            "Data de Admissao: 05/05/2026\n"
            "Endereco: Rua das Flores, 123 - Centro - Sao Paulo/SP\n"
            "CEP: 01000-000\n"
            "Telefone: (11) 99999-9999\n"
            "Cargo: Analista de Operacoes"
        )
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Ana Paula Souza")
        self.assertTrue(any("Endereco:" in note for note in result.task.notes))
        self.assertTrue(any("CEP:" in note for note in result.task.notes))
        self.assertTrue(any("Telefone:" in note for note in result.task.notes))

    def test_parse_inline_offboarding(self) -> None:
        message = build_message("Offboarding Joao Souza dia 30/04/2026. Vai devolver por Uber.")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.task_type, TaskType.OFFBOARDING)
        self.assertEqual(result.task.employee_name, "Joao Souza")
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-30")

    def test_parse_multiple_names_from_bullet_list(self) -> None:
        message = build_message(
            "Proximos Onboardings (04/05)\n\n- Maria Eduarda de Castro das Neves Silva\n- Ana Julia Simoes Silverio"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 2)
        self.assertEqual(result.tasks[0].employee_name, "Maria Eduarda de Castro das Neves Silva")
        self.assertEqual(result.tasks[1].employee_name, "Ana Julia Simoes Silverio")
        self.assertTrue(all(task.effective_date.isoformat() == "2026-05-04" for task in result.tasks))

    def test_parse_multiple_names_from_single_bullet(self) -> None:
        message = build_message(
            "Onboarding 27/04:\n- Maiara Barreto e Joao Pedro\nVao precisar de notebook e perifericos"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 2)
        self.assertEqual(result.tasks[0].employee_name, "Maiara Barreto")
        self.assertEqual(result.tasks[1].employee_name, "Joao Pedro")

    def test_skip_when_date_is_missing(self) -> None:
        message = build_message("Onboarding de Ana Paula. Quer mouse e teclado.")
        result = self.parser.parse_message(message)

        self.assertIsNone(result.task)
        self.assertEqual(result.reason, "data nao identificada")


if __name__ == "__main__":
    unittest.main()
