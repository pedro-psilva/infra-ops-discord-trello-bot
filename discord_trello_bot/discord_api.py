from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

from .config import Settings
from .http import JsonApiClient
from .models import DiscordMessage


LOGGER = logging.getLogger(__name__)


class DiscordApiClient(JsonApiClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            base_url=settings.discord_api_base_url,
            default_headers={
                "Authorization": f"Bot {settings.discord_bot_token}",
                "User-Agent": "discord-trello-bot/1.0",
            },
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self.settings = settings

    def list_channel_messages(
        self,
        channel_id: str,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> list[DiscordMessage]:
        params: dict[str, object] = {"limit": limit}
        if before:
            params["before"] = before
        payload = self.request("GET", f"channels/{channel_id}/messages", params=params)
        return [DiscordMessage.from_api(item) for item in payload]

    def iter_messages_since(self, channel_id: str, cutoff: datetime) -> list[DiscordMessage]:
        before: str | None = None
        collected: list[DiscordMessage] = []

        while len(collected) < self.settings.max_messages_per_channel:
            remaining = self.settings.max_messages_per_channel - len(collected)
            batch = self.list_channel_messages(channel_id, before=before, limit=min(100, remaining))
            if not batch:
                break

            reached_cutoff = False
            for message in batch:
                if message.timestamp >= cutoff:
                    collected.append(message)
                else:
                    reached_cutoff = True

            if reached_cutoff:
                break

            before = batch[-1].id
            if len(batch) < 100:
                break

        if len(collected) == self.settings.max_messages_per_channel:
            LOGGER.warning(
                "Canal %s atingiu o limite MAX_MESSAGES_PER_CHANNEL=%s.",
                channel_id,
                self.settings.max_messages_per_channel,
            )

        collected.sort(key=lambda item: item.timestamp)
        return collected

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        encoded_emoji = quote(emoji, safe="")
        self.request(
            "PUT",
            f"channels/{channel_id}/messages/{message_id}/reactions/{encoded_emoji}/@me",
            headers={"Content-Length": "0"},
        )

    def reply_to_message(self, channel_id: str, message_id: str, content: str) -> None:
        self.request(
            "POST",
            f"channels/{channel_id}/messages",
            json_body={
                "content": content,
                "allowed_mentions": {"parse": []},
                "message_reference": {
                    "message_id": message_id,
                    "fail_if_not_exists": False,
                },
            },
        )


def build_discord_message_url(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
