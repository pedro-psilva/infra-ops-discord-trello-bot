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

    def test_parse_onboarding_strips_trailing_punctuation_from_name(self) -> None:
        message = build_message("Onboarding Ana Júlia! Data de Admissao: 04/05/2026")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Ana Júlia")

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

    def test_parse_offboarding_email_with_today_and_greeting_name(self) -> None:
        message = build_message(
            "[Infra] Orientacoes ao processo de Offboarding\n"
            "Boa tarde, Jucilene! Como voce esta?\n\n"
            "Venho trazer mais informacoes sobre os proximos passos no processo de desligamento, "
            "visto que seu ultimo dia trabalhado sera hoje."
        )
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.task_type, TaskType.OFFBOARDING)
        self.assertEqual(result.task.employee_name, "Jucilene")
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-17")

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

    def test_explicit_day_month_uses_message_year(self) -> None:
        message = build_message("Offboarding Joao Souza dia 14/04. Vai devolver por Uber.")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-14")

    def test_parse_offboarding_name_before_dash_date(self) -> None:
        message = build_message("Offboarding Jucilene Aparecida - 27/04")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.task_type, TaskType.OFFBOARDING)
        self.assertEqual(result.task.employee_name, "Jucilene Aparecida")
        self.assertEqual(result.task.effective_date.isoformat(), "2026-04-27")


class CleanNameParentheticalTests(unittest.TestCase):
    """Garante que anotacoes parenteticas no final do nome sao removidas."""

    def setUp(self) -> None:
        self.parser = TaskParser(build_settings())

    def test_parenthetical_annotation_stripped(self) -> None:
        message = build_message(
            "Onboarding\nData (11/05)\n- Joao Silva (e de BH)"
        )
        result = self.parser.parse_message(message)
        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Joao Silva")

    def test_parenthetical_company_annotation_stripped(self) -> None:
        message = build_message(
            "Onboarding\nData: 11/05\n- Carlos Eduardo Melo (Mercantil)"
        )
        result = self.parser.parse_message(message)
        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Carlos Eduardo Melo")

    def test_name_without_parenthetical_unchanged(self) -> None:
        message = build_message(
            "Onboarding\nNome: Ana Paula Ferreira\nData: 15/05"
        )
        result = self.parser.parse_message(message)
        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Ana Paula Ferreira")


class MultiDateSectionParserTests(unittest.TestCase):
    """Garante que mensagens com multiplos cabecalhos 'Data (dd/mm)' geram
    cards com as datas corretas para cada secao."""

    def setUp(self) -> None:
        self.parser = TaskParser(build_settings())

    def test_two_date_sections_produce_tasks_with_correct_dates(self) -> None:
        message = build_message(
            "Onboardings proximos:\n\n"
            "Data (07/07)\n"
            "- Alice Mendonca\n"
            "- Bruno Carvalho\n\n"
            "Data (11/05)\n"
            "- Carlos Duarte\n"
            "- Diana Rocha\n"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 4)
        names = [t.employee_name for t in result.tasks]
        self.assertIn("Alice Mendonca", names)
        self.assertIn("Bruno Carvalho", names)
        self.assertIn("Carlos Duarte", names)
        self.assertIn("Diana Rocha", names)

        date_section1 = [t for t in result.tasks if t.employee_name in ("Alice Mendonca", "Bruno Carvalho")]
        date_section2 = [t for t in result.tasks if t.employee_name in ("Carlos Duarte", "Diana Rocha")]
        self.assertTrue(all(t.effective_date.isoformat() == "2026-07-07" for t in date_section1))
        self.assertTrue(all(t.effective_date.isoformat() == "2026-05-11" for t in date_section2))

    def test_two_date_sections_with_parenthetical_annotations(self) -> None:
        message = build_message(
            "Onboardings proximos:\n\n"
            "Data (07/07)\n"
            "- Ana Lima\n"
            "- Beatriz Costa\n"
            "- Carla Moura\n"
            "- Diego Pinto\n"
            "- Eduardo Farias\n\n"
            "Data (11/05)\n"
            "- Felipe Gomes (e de BH)\n"
            "- Gabriela Souza (Mercantil)\n"
            "- Henrique Nunes\n"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 8)
        names = [t.employee_name for t in result.tasks]
        self.assertIn("Felipe Gomes", names)
        self.assertIn("Gabriela Souza", names)
        self.assertIn("Henrique Nunes", names)
        self.assertNotIn("Felipe Gomes (e de BH)", names)
        self.assertNotIn("Gabriela Souza (Mercantil)", names)

        second_section_tasks = [
            t for t in result.tasks
            if t.employee_name in ("Felipe Gomes", "Gabriela Souza", "Henrique Nunes")
        ]
        self.assertTrue(all(t.effective_date.isoformat() == "2026-05-11" for t in second_section_tasks))

    def test_single_date_section_falls_back_to_normal_parsing(self) -> None:
        message = build_message(
            "Onboardings proximos:\n\n"
            "Data (07/07)\n"
            "- Ana Lima\n"
            "- Beatriz Costa\n"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 2)
        self.assertTrue(all(t.effective_date.isoformat() == "2026-07-07" for t in result.tasks))

    def test_bold_date_section_headers_recognized(self) -> None:
        message = build_message(
            "Onboardings proximos:\n\n"
            "**Data (07/07)**\n"
            "- Ana Lima\n\n"
            "**Data (11/05)**\n"
            "- Bruno Carvalho\n"
        )
        result = self.parser.parse_message(message)

        self.assertEqual(len(result.tasks), 2)
        ana = next(t for t in result.tasks if t.employee_name == "Ana Lima")
        bruno = next(t for t in result.tasks if t.employee_name == "Bruno Carvalho")
        self.assertEqual(ana.effective_date.isoformat(), "2026-07-07")
        self.assertEqual(bruno.effective_date.isoformat(), "2026-05-11")




class CargoExtractionTests(unittest.TestCase):
    """Garante que o cargo é extraído corretamente do texto da mensagem."""

    def setUp(self) -> None:
        self.parser = TaskParser(build_settings())

    def test_cargo_extracted_from_asterisk_format(self) -> None:
        """Onboarding DD/MM * Nome - Cargo (Local) -> cargo detectado."""
        message = build_message("Onboarding 20/05 * Jonathan Tavares - Tech Lead Bamaq")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Jonathan Tavares")
        self.assertEqual(result.task.cargo, "Tech Lead Bamaq")

    def test_cargo_extracted_with_location_parenthetical(self) -> None:
        """Onboarding DD/MM * Nome - Cargo (Local) -> parentético de local não entra no cargo."""
        message = build_message("Offboarding 11/05 * Gustavo Lucio Pereira (n é de bh)")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Gustavo Lucio Pereira")
        # Sem " - Cargo" no formato acima, cargo deve ser None
        self.assertIsNone(result.task.cargo)

    def test_cargo_extracted_from_dash_separator(self) -> None:
        """Nome - Cargo sem asterisco também extrai o cargo."""
        message = build_message("Onboarding\nNome: Jonathan Tavares - Tech Lead\nData: 20/05/2026")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Jonathan Tavares")
        self.assertEqual(result.task.cargo, "Tech Lead")

    def test_cargo_none_when_no_separator(self) -> None:
        """Sem separador ' - Cargo', cargo deve ser None."""
        message = build_message(
            "Onboarding\nNome: Maria Silva\nData: 22/04/2026"
        )
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Maria Silva")
        self.assertIsNone(result.task.cargo)

    def test_cargo_not_extracted_when_separator_followed_by_date(self) -> None:
        """' - DD/MM' não deve ser interpretado como cargo."""
        message = build_message("Offboarding Jucilene Aparecida - 27/04")
        result = self.parser.parse_message(message)

        self.assertIsNotNone(result.task)
        assert result.task is not None
        self.assertEqual(result.task.employee_name, "Jucilene Aparecida")
        self.assertIsNone(result.task.cargo)

if __name__ == "__main__":
    unittest.main()
