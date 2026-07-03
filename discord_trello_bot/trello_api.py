from __future__ import annotations

import logging
import re
import threading
from datetime import date

from .config import Settings


LOGGER = logging.getLogger(__name__)
from .http import JsonApiClient
from .models import TaskCard, TaskType


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
        self._target_list_cards_cache: list[dict] | None = None
        self._board_labels_cache: list[dict] | None = None
        self._cache_lock = threading.RLock()

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
        card = self.request("POST", "cards", params=params)
        self._remember_target_list_card(card)
        return card

    def find_open_card_by_name(self, card_name: str) -> dict | None:
        target_name = card_name.casefold()
        with self._cache_lock:
            self._load_target_list_cards()
            for card in self._target_list_cards_cache or []:
                if str(card.get("name", "")).casefold() == target_name:
                    return card
        return None

    def list_open_task_cards(self, task_type: TaskType | None = None) -> list[TaskCard]:
        with self._cache_lock:
            self._load_target_list_cards()
            source = list(self._target_list_cards_cache or [])
        cards: list[TaskCard] = []
        for card in source:
            task_card = _parse_task_card(card)
            if task_card is None:
                continue
            if task_type is not None and task_card.task_type is not task_type:
                continue
            cards.append(task_card)
        return cards

    def add_comment(self, *, card_id: str, text: str) -> dict:
        params = self._with_auth({"text": text})
        return self.request("POST", f"cards/{card_id}/actions/comments", params=params)

    def get_card(self, card_id: str, *, fields: str = "id,name,url,desc") -> dict:
        return self.request(
            "GET",
            f"cards/{card_id}",
            params=self._with_auth({"fields": fields}),
        )

    def update_card_description(self, *, card_id: str, desc: str) -> dict:
        return self.request(
            "PUT",
            f"cards/{card_id}",
            params=self._with_auth({"desc": desc}),
        )

    def create_card(
        self,
        *,
        card_name: str,
        due_iso: str | None = None,
        desc: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict:
        params: dict[str, object] = {
            "idList": self.resolve_target_list_id(),
            "name": card_name,
        }
        if due_iso is not None:
            params["due"] = due_iso
        if desc:
            params["desc"] = desc
        if label_ids:
            params["idLabels"] = ",".join(label_ids)
        card = self.request("POST", "cards", params=self._with_auth(params))
        self._remember_target_list_card(card)
        return card

    def list_board_labels(self) -> list[dict]:
        with self._cache_lock:
            if self._board_labels_cache is None:
                board_id = self._resolve_board_id()
                labels = self.request(
                    "GET",
                    f"boards/{board_id}/labels",
                    params=self._with_auth({"fields": "id,name", "limit": "1000"}),
                )
                self._board_labels_cache = [
                    {"id": str(label["id"]), "name": str(label.get("name") or "").strip()}
                    for label in labels
                    if str(label.get("name") or "").strip()
                ]
            return self._board_labels_cache

    def label_ids_for_names(self, names) -> list[str]:
        wanted = [str(name).strip().casefold() for name in names if str(name).strip()]
        if not wanted:
            return []
        by_name = {label["name"].casefold(): label["id"] for label in self.list_board_labels()}
        ids: list[str] = []
        for name in wanted:
            label_id = by_name.get(name)
            if label_id and label_id not in ids:
                ids.append(label_id)
        return ids

    def _resolve_board_id(self) -> str:
        if self.settings.trello_board_ref:
            return _extract_board_ref(self.settings.trello_board_ref)
        list_info = self.request(
            "GET",
            f"lists/{self.resolve_target_list_id()}",
            params=self._with_auth({"fields": "idBoard"}),
        )
        return str(list_info["idBoard"])

    def _with_auth(self, params: dict[str, object]) -> dict[str, object]:
        return {
            **params,
            "key": self.settings.trello_api_key,
            "token": self.settings.trello_api_token,
        }

    def _remember_target_list_card(self, card: dict) -> None:
        with self._cache_lock:
            if self._target_list_cards_cache is not None:
                self._target_list_cards_cache.append(card)

    def _load_target_list_cards(self) -> None:
        with self._cache_lock:
            if self._target_list_cards_cache is not None:
                return
            target_list_id = self.resolve_target_list_id()
            self._target_list_cards_cache = self.request(
                "GET",
                f"lists/{target_list_id}/cards",
                params=self._with_auth({"fields": "id,name,url", "filter": "open"}),
            )

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


def _parse_task_card(card: dict) -> TaskCard | None:
    name = str(card.get("name", ""))
    match = re.match(
        r"^\[(?P<type>Onboarding|Offboarding)\]\s+(?P<employee>.+?)\s+-\s+(?P<day>\d{2})/(?P<month>\d{2})/(?P<year>\d{4})$",
        name,
        re.IGNORECASE,
    )
    if not match:
        LOGGER.debug("Card '%s' ignorado: nome fora do padrao esperado.", name)
        return None

    day = int(match.group("day"))
    month = int(match.group("month"))
    year = int(match.group("year"))
    task_type = (
        TaskType.ONBOARDING
        if match.group("type").casefold() == "onboarding"
        else TaskType.OFFBOARDING
    )
    return TaskCard(
        id=str(card["id"]),
        url=str(card["url"]),
        name=name,
        task_type=task_type,
        employee_name=match.group("employee").strip(),
        effective_date=date(year, month, day),
    )
