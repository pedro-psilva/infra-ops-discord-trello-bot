from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfirmationMode(StrEnum):
    REACTION = "reaction"
    REPLY = "reply"
    BOTH = "both"


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    discord_guild_id: str
    discord_channel_ids: tuple[str, ...]
    discord_request_channel_ids: tuple[str, ...]
    discord_confirmation_mode: ConfirmationMode
    discord_reaction_emoji: str
    discord_reply_template: str
    trello_api_key: str
    trello_api_token: str
    trello_board_ref: str | None
    trello_target_list_id: str | None
    trello_target_list_name: str | None
    trello_onboarding_template_card_ref: str
    trello_offboarding_template_card_ref: str
    trello_keep_from_source: str
    timezone: ZoneInfo
    lookback_days: int
    max_messages_per_channel: int
    log_level: str
    discord_api_base_url: str = "https://discord.com/api/v10"
    trello_api_base_url: str = "https://api.trello.com/1"
    request_timeout_seconds: float = 30.0
    max_retries: int = 5
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.5"
    openai_reasoning_effort: str = "medium"
    openai_api_base_url: str = "https://api.openai.com/v1"
    gmail_user_email: str | None = None
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    gmail_refresh_token: str | None = None
    gmail_query: str = "newer_than:7d (onboarding OR offboarding OR admissao OR admissão OR contratação OR desligamento OR subject:Offboarding)"
    gmail_processed_label_name: str = "Infra Ops Processado"
    gmail_max_results: int = 10


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"A variavel de ambiente obrigatoria {name} nao foi definida.")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _csv_env(name: str) -> tuple[str, ...]:
    raw = _require_env(name)
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not values:
        raise ValueError(f"A variavel de ambiente {name} precisa ter ao menos um valor.")
    return values


def _csv_optional_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"A variavel de ambiente {name} precisa ser um inteiro.") from exc
    if value <= 0:
        raise ValueError(f"A variavel de ambiente {name} precisa ser maior que zero.")
    return value


def _timezone_env(name: str, default: str) -> ZoneInfo:
    raw = os.getenv(name, "").strip() or default
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Timezone invalida em {name}: {raw}. Use uma timezone IANA, por exemplo America/Sao_Paulo."
        ) from exc


def _confirmation_mode_env() -> ConfirmationMode:
    raw = (
        os.getenv("DISCORD_CONFIRMATION_MODE", "").strip().lower()
        or ConfirmationMode.REACTION.value
    )
    try:
        return ConfirmationMode(raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ConfirmationMode)
        raise ValueError(
            f"DISCORD_CONFIRMATION_MODE invalido: {raw}. Valores aceitos: {allowed}."
        ) from exc


def _openai_reasoning_effort_env() -> str:
    raw = os.getenv("OPENAI_REASONING_EFFORT", "medium").strip().lower() or "medium"
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    if raw not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(
            "OPENAI_REASONING_EFFORT invalido: "
            f"{raw}. Valores aceitos: {allowed_values}."
        )
    return raw


def _gmail_env() -> tuple[str | None, str | None, str | None, str | None]:
    user_email = _optional_env("GMAIL_USER_EMAIL")
    client_id = _optional_env("GMAIL_CLIENT_ID")
    client_secret = _optional_env("GMAIL_CLIENT_SECRET")
    refresh_token = _optional_env("GMAIL_REFRESH_TOKEN")
    values = (user_email, client_id, client_secret, refresh_token)
    if any(values) and not all(values):
        raise ValueError(
            "Para ativar Gmail, defina GMAIL_USER_EMAIL, GMAIL_CLIENT_ID, "
            "GMAIL_CLIENT_SECRET e GMAIL_REFRESH_TOKEN."
        )
    return values


def load_settings(*, lookback_days_override: int | None = None) -> Settings:
    lookback_days = lookback_days_override or _int_env("LOOKBACK_DAYS", 7)
    gmail_user_email, gmail_client_id, gmail_client_secret, gmail_refresh_token = _gmail_env()
    discord_channel_ids = _csv_optional_env("DISCORD_CHANNEL_IDS")
    discord_request_channel_ids = _csv_optional_env("DISCORD_REQUEST_CHANNEL_IDS")
    if not discord_channel_ids and not discord_request_channel_ids:
        raise ValueError(
            "Defina DISCORD_CHANNEL_IDS e/ou DISCORD_REQUEST_CHANNEL_IDS com ao menos um canal."
        )
    trello_target_list_id = _optional_env("TRELLO_TARGET_LIST_ID")
    trello_target_list_name = _optional_env("TRELLO_TARGET_LIST_NAME")
    trello_board_ref = _optional_env("TRELLO_BOARD_REF")
    if not trello_target_list_id and not (trello_board_ref and trello_target_list_name):
        raise ValueError(
            "Defina TRELLO_TARGET_LIST_ID ou entao TRELLO_BOARD_REF junto com TRELLO_TARGET_LIST_NAME."
        )
    return Settings(
        discord_bot_token=_require_env("DISCORD_BOT_TOKEN"),
        discord_guild_id=_require_env("DISCORD_GUILD_ID"),
        discord_channel_ids=discord_channel_ids,
        discord_request_channel_ids=discord_request_channel_ids,
        discord_confirmation_mode=_confirmation_mode_env(),
        discord_reaction_emoji=os.getenv("DISCORD_REACTION_EMOJI", "✅").strip() or "✅",
        discord_reply_template=os.getenv(
            "DISCORD_REPLY_TEMPLATE",
            "Card criado no Trello: {card_url}",
        ).strip()
        or "Card criado no Trello: {card_url}",
        trello_api_key=_require_env("TRELLO_API_KEY"),
        trello_api_token=_require_env("TRELLO_API_TOKEN"),
        trello_board_ref=trello_board_ref,
        trello_target_list_id=trello_target_list_id,
        trello_target_list_name=trello_target_list_name,
        trello_onboarding_template_card_ref=_require_env("TRELLO_ONBOARDING_TEMPLATE_CARD_REF"),
        trello_offboarding_template_card_ref=_require_env("TRELLO_OFFBOARDING_TEMPLATE_CARD_REF"),
        trello_keep_from_source=os.getenv(
            "TRELLO_KEEP_FROM_SOURCE",
            "checklists,customFields,labels",
        ).strip()
        or "checklists,customFields,labels",
        timezone=_timezone_env("BOT_TIMEZONE", "America/Sao_Paulo"),
        lookback_days=lookback_days,
        max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 500),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        openai_api_key=_optional_env("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5",
        openai_reasoning_effort=_openai_reasoning_effort_env(),
        gmail_user_email=gmail_user_email,
        gmail_client_id=gmail_client_id,
        gmail_client_secret=gmail_client_secret,
        gmail_refresh_token=gmail_refresh_token,
        gmail_query=os.getenv(
            "GMAIL_QUERY",
            "newer_than:7d (onboarding OR offboarding OR admissao OR admissão OR contratação OR desligamento OR subject:Offboarding)",
        ).strip()
        or "newer_than:7d (onboarding OR offboarding OR admissao OR admissão OR contratação OR desligamento OR subject:Offboarding)",
        gmail_processed_label_name=os.getenv(
            "GMAIL_PROCESSED_LABEL_NAME",
            "Infra Ops Processado",
        ).strip()
        or "Infra Ops Processado",
        gmail_max_results=_int_env("GMAIL_MAX_RESULTS", 10),
    )
