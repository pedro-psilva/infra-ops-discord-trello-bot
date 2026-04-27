from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from discord_trello_bot.config import Settings, load_settings
from discord_trello_bot.service import DiscordTrelloService


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Processa mensagens do Discord e cria cards no Trello."
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Sobrescreve o LOOKBACK_DAYS apenas para esta execucao.",
    )
    return parser


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    load_dotenv()
    args = build_argument_parser().parse_args()
    settings = load_settings(lookback_days_override=args.lookback_days)
    configure_logging(settings)

    service = DiscordTrelloService(settings)
    summary = service.run()

    logging.getLogger(__name__).info(
        "Execucao concluida. canais=%s mensagens=%s emails=%s tarefas=%s cards=%s ja_confirmadas=%s ignoradas=%s erros=%s",
        summary.channels_scanned,
        summary.messages_scanned,
        summary.emails_scanned,
        summary.tasks_parsed,
        summary.cards_created,
        summary.messages_already_confirmed,
        summary.messages_skipped,
        summary.errors,
    )
    return 0 if summary.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
