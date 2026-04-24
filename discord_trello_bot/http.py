from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiError(RuntimeError):
    status_code: int
    message: str
    body: str

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.message}"


class JsonApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        default_headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(default_headers or {})

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body: str | None = None

        for attempt in range(1, self.max_retries + 1):
            response = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout_seconds,
            )

            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = self._retry_after_seconds(response)
                LOGGER.warning("Rate limit em %s. Aguardando %ss.", url, retry_after)
                time.sleep(retry_after)
                continue

            if response.status_code >= 500 and attempt < self.max_retries:
                retry_after = min(2**attempt, 30)
                LOGGER.warning(
                    "Erro %s em %s. Nova tentativa em %ss.",
                    response.status_code,
                    url,
                    retry_after,
                )
                time.sleep(retry_after)
                continue

            if response.status_code >= 400:
                body = response.text.strip()
                raise ApiError(
                    status_code=response.status_code,
                    message=f"Falha na chamada {method.upper()} {url}",
                    body=body,
                )

            if response.status_code == 204 or not response.content:
                return None

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type or response.text.startswith("{") or response.text.startswith("["):
                return response.json()

            return response.text

        if body is None:
            body = "A requisicao excedeu o numero maximo de tentativas."
        raise ApiError(status_code=500, message=f"Falha persistente em {url}", body=body)

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> float:
        header_value = response.headers.get("Retry-After")
        if header_value:
            try:
                return max(float(header_value), 1.0)
            except ValueError:
                pass

        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {}

        retry_after = payload.get("retry_after", 1)
        try:
            return max(float(retry_after), 1.0)
        except (TypeError, ValueError):
            return 1.0
