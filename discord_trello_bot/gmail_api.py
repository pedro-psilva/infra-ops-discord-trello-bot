from __future__ import annotations

import base64
import logging
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .config import Settings


LOGGER = logging.getLogger(__name__)
from .http import ApiError, JsonApiClient
from .models import EmailMessage


class GmailApiClient:
    def __init__(self, settings: Settings) -> None:
        if not (
            settings.gmail_user_email
            and settings.gmail_client_id
            and settings.gmail_client_secret
            and settings.gmail_refresh_token
        ):
            raise ValueError("Credenciais do Gmail nao configuradas.")
        self.settings = settings
        self._access_token: str | None = None
        self._processed_label_id: str | None = None

    def list_messages(self) -> list[EmailMessage]:
        message_refs = self._client().request(
            "GET",
            f"/gmail/v1/users/{self.settings.gmail_user_email}/messages",
            params={
                "q": self.settings.gmail_query,
                "maxResults": self.settings.gmail_max_results,
            },
        )
        refs = message_refs.get("messages", []) if isinstance(message_refs, dict) else []
        if isinstance(message_refs, dict) and "messages" not in message_refs:
            LOGGER.debug("Gmail retornou resposta sem campo 'messages' para query '%s'.", self.settings.gmail_query)
        return [self.get_message(str(ref["id"])) for ref in refs if ref.get("id")]

    def get_message(self, message_id: str) -> EmailMessage:
        payload = self._client().request(
            "GET",
            f"/gmail/v1/users/{self.settings.gmail_user_email}/messages/{message_id}",
            params={"format": "full"},
        )
        headers = _headers_by_name(payload.get("payload", {}).get("headers", []))
        timestamp = _message_timestamp(payload=payload, headers=headers)
        body = _extract_body(payload.get("payload", {}))
        return EmailMessage(
            id=str(payload["id"]),
            thread_id=str(payload.get("threadId") or ""),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            body=body,
            timestamp=timestamp,
            label_ids=tuple(str(label_id) for label_id in payload.get("labelIds", [])),
            recipient=headers.get("to", ""),
        )

    def processed_label_id(self) -> str:
        if self._processed_label_id is None:
            self._processed_label_id = self._resolve_label_id(self.settings.gmail_processed_label_name)
        return self._processed_label_id

    def mark_processed(self, message_id: str) -> None:
        self._client().request(
            "POST",
            f"/gmail/v1/users/{self.settings.gmail_user_email}/messages/{message_id}/modify",
            json_body={"addLabelIds": [self.processed_label_id()]},
        )

    def _resolve_label_id(self, label_name: str) -> str:
        payload = self._client().request(
            "GET",
            f"/gmail/v1/users/{self.settings.gmail_user_email}/labels",
        )
        for label in payload.get("labels", []):
            if str(label.get("name", "")).lower() == label_name.lower():
                return str(label["id"])

        created = self._client().request(
            "POST",
            f"/gmail/v1/users/{self.settings.gmail_user_email}/labels",
            json_body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        return str(created["id"])

    def _client(self) -> JsonApiClient:
        if self._access_token is None:
            self._access_token = self._refresh_access_token()
        return JsonApiClient(
            base_url="https://gmail.googleapis.com",
            default_headers={"Authorization": f"Bearer {self._access_token}"},
            timeout_seconds=self.settings.request_timeout_seconds,
            max_retries=self.settings.max_retries,
        )

    def _refresh_access_token(self) -> str:
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.settings.gmail_client_id,
                    "client_secret": self.settings.gmail_client_secret,
                    "refresh_token": self.settings.gmail_refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ApiError(
                status_code=0,
                message="Falha de conexao ao renovar access token do Gmail.",
                body=str(exc),
            ) from exc
        if response.status_code >= 400:
            raise ApiError(
                status_code=response.status_code,
                message="Falha ao renovar access token do Gmail.",
                body=response.text.strip(),
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ValueError("Google nao retornou access_token ao renovar token do Gmail.")
        return str(token)


def _headers_by_name(headers: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
    }


def _message_timestamp(*, payload: dict[str, Any], headers: dict[str, str]) -> datetime:
    internal_date = payload.get("internalDate")
    if internal_date:
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
    date_header = headers.get("date")
    if date_header:
        parsed = parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(tz=timezone.utc)


def _extract_body(part: dict[str, Any]) -> str:
    if part.get("mimeType") == "text/plain":
        body = _decode_body(part.get("body", {}).get("data"))
        if body:
            return body

    child_parts = part.get("parts") or []
    for child in child_parts:
        if child.get("mimeType") == "text/plain":
            body = _extract_body(child)
            if body:
                return body
    for child in child_parts:
        body = _extract_body(child)
        if body:
            return body

    if part.get("mimeType") == "text/html":
        return _html_to_text(_decode_body(part.get("body", {}).get("data")))

    return _decode_body(part.get("body", {}).get("data"))


def _decode_body(value: str | None) -> str:
    if not value:
        return ""
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    return decoded.decode("utf-8", errors="replace").strip()


def _html_to_text(value: str) -> str:
    without_tags = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    without_tags = re.sub(r"</p\s*>", "\n", without_tags, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", without_tags)
    return html.unescape(re.sub(r"[ \t]+", " ", without_tags)).strip()
