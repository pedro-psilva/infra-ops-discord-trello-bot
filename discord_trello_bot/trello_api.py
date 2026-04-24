from __future__ import annotations

import re

from .config import Settings
from .http import JsonApiClient
from .models import TaskType


class TrelloApiClient(JsonApiClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            base_url=settings.trello_api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self.settings = settings
        self._resolved_target_list_id: str | None = None
        self._resolved_onboarding_template_id: str | None = None
        self._resolved_offboarding_template_id: str | None = None

    def create_card_from_template(
        self,
        *,
        card_name: str,
        due_iso: str,
        task_type: TaskType,
    ) -> dict:
        template_card_id = self.resolve_template_card_id(task_type)
        params = self._with_auth(
            {
                "idList": self.resolve_target_list_id(),
                "idCardSource": template_card_id,
                "keepFromSource": self.settings.trello_keep_from_source,
                "name": card_name,
                "due": due_iso,
            }
        )
        return self.request("POST", "cards", params=params)

    def add_comment(self, *, card_id: str, text: str) -> dict:
        params = self._with_auth({"text": text})
        return self.request("POST", f"cards/{card_id}/actions/comments", params=params)

    def _with_auth(self, params: dict[str, object]) -> dict[str, object]:
        return {
            **params,
            "key": self.settings.trello_api_key,
            "token": self.settings.trello_api_token,
        }

    def resolve_target_list_id(self) -> str:
        if self._resolved_target_list_id:
            return self._resolved_target_list_id

        if self.settings.trello_target_list_id:
            self._resolved_target_list_id = self.settings.trello_target_list_id
            return self._resolved_target_list_id

        assert self.settings.trello_board_ref is not None
        assert self.settings.trello_target_list_name is not None

        board_ref = _extract_board_ref(self.settings.trello_board_ref)
        lists = self.request(
            "GET",
            f"boards/{board_ref}/lists",
            params=self._with_auth({"fields": "id,name", "filter": "open"}),
        )

        target_name = self.settings.trello_target_list_name.casefold()
        matches = [item for item in lists if str(item.get("name", "")).casefold() == target_name]
        if not matches:
            available = ", ".join(str(item.get("name", "")) for item in lists)
            raise ValueError(
                f'Nao encontrei a lista "{self.settings.trello_target_list_name}" no board informado. '
                f"Listas abertas encontradas: {available}"
            )
        if len(matches) > 1:
            raise ValueError(
                f'Encontrei mais de uma lista com o nome "{self.settings.trello_target_list_name}". '
                "Use TRELLO_TARGET_LIST_ID para desambiguar."
            )

        self._resolved_target_list_id = str(matches[0]["id"])
        return self._resolved_target_list_id

    def resolve_template_card_id(self, task_type: TaskType) -> str:
        if task_type is TaskType.ONBOARDING:
            if self._resolved_onboarding_template_id is None:
                self._resolved_onboarding_template_id = self._resolve_card_ref(
                    self.settings.trello_onboarding_template_card_ref
                )
            return self._resolved_onboarding_template_id

        if self._resolved_offboarding_template_id is None:
            self._resolved_offboarding_template_id = self._resolve_card_ref(
                self.settings.trello_offboarding_template_card_ref
            )
        return self._resolved_offboarding_template_id

    def _resolve_card_ref(self, ref: str) -> str:
        card_ref = _extract_card_ref(ref)
        payload = self.request(
            "GET",
            f"cards/{card_ref}",
            params=self._with_auth({"fields": "id"}),
        )
        return str(payload["id"])


def _extract_board_ref(value: str) -> str:
    value = value.strip()
    match = re.search(r"trello\.com/b/([A-Za-z0-9]+)/?", value, re.IGNORECASE)
    if match:
        return match.group(1)
    return value


def _extract_card_ref(value: str) -> str:
    value = value.strip()
    match = re.search(r"trello\.com/c/([A-Za-z0-9]+)/?", value, re.IGNORECASE)
    if match:
        return match.group(1)
    return value
