from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime
from hashlib import sha256

from .config import Settings
from .http import JsonApiClient
from .models import DiscordMessage, RequestedCard


LOGGER = logging.getLogger(__name__)

REQUESTED_CARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "details", "due_date", "labels"],
    "properties": {
        "title": {
            "type": "string",
            "description": "Titulo intuitivo e objetivo para o card, em portugues do Brasil.",
        },
        "summary": {
            "type": "string",
            "description": "Resumo fiel da solicitacao principal em 1 ou 2 frases curtas.",
        },
        "details": {
            "type": "string",
            "description": "Detalhes importantes em no maximo 2 frases curtas, sem citar mensagens ou autores.",
        },
        "due_date": {
            "type": "string",
            "description": "Prazo em 'YYYY-MM-DD' apenas se estiver explicito no pedido ou no contexto; caso contrario vazio.",
        },
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nomes das tags aplicaveis, escolhidos apenas entre as tags disponiveis. Vazio se nenhuma se aplica.",
        },
    },
}

_SYSTEM_INSTRUCTIONS = (
    "Voce transforma uma conversa do Discord em um card do Trello em portugues do Brasil. "
    "Seja fiel ao contexto, nao invente fatos, nao atribua passos ou decisoes que nao estejam "
    "claros, e nao cite mensagens, timestamps ou autores no resultado final. "
    "Produza um titulo intuitivo, um resumo objetivo da demanda principal e detalhes importantes "
    "que ajudem a execucao. Se algum detalhe adicional nao for util, devolva details vazio. "
    "Escreva em português do Brasil correto, com acentuação e ortografia adequadas, mesmo que o "
    "contexto venha sem acentos."
)


class CardRefiner:
    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY nao configurada.")
        self.settings = settings
        self.client = JsonApiClient(
            base_url=settings.anthropic_api_base_url,
            default_headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": settings.anthropic_version,
                "Content-Type": "application/json",
            },
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )

    def refine(
        self,
        *,
        requested_card: RequestedCard,
        command_message: DiscordMessage,
        context_messages: list[DiscordMessage],
        today: date | None = None,
        available_labels: list[str] | None = None,
    ) -> RequestedCard:
        label_names = list(available_labels or [])
        payload = {
            "model": self.settings.anthropic_model,
            "max_tokens": 1024,
            "system": _build_system(today, label_names),
            "metadata": {"user_id": _hash_author(command_message.author_id)},
            "messages": [
                {
                    "role": "user",
                    "content": self._build_prompt(
                        requested_card=requested_card,
                        command_message=command_message,
                        context_messages=context_messages,
                    ),
                }
            ],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": _build_schema(label_names),
                }
            },
        }

        response = self.client.request("POST", "/messages", json_body=payload)
        output_text = _extract_output_text(response)
        if not output_text:
            raise ValueError("Resposta vazia da Anthropic ao refinar o card.")

        try:
            data = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ValueError("A Anthropic nao retornou JSON valido para o card.") from exc

        title = _clean_text(str(data.get("title") or ""), limit=90)
        summary = _clean_text(str(data.get("summary") or ""), limit=600)
        details = _clean_text(str(data.get("details") or ""), limit=600)

        if not title or not summary:
            raise ValueError("A Anthropic retornou card sem titulo ou resumo.")

        if details == summary or details in summary:
            details = ""

        due_date = requested_card.due_date or _parse_iso_date(str(data.get("due_date") or "").strip())
        labels = _parse_labels(data.get("labels"))

        return RequestedCard(
            title=title,
            summary=summary,
            due_date=due_date,
            instruction=requested_card.instruction,
            source_excerpt=requested_card.source_excerpt,
            context_excerpt=details,
            labels=labels,
        )

    def _build_prompt(
        self,
        *,
        requested_card: RequestedCard,
        command_message: DiscordMessage,
        context_messages: list[DiscordMessage],
    ) -> str:
        due_label = requested_card.due_date.strftime("%d/%m/%Y") if requested_card.due_date else "Sem prazo definido"
        transcript = _build_transcript(context_messages=context_messages, command_message=command_message)
        return (
            "Monte um card de Trello com base no pedido abaixo.\n\n"
            f"Solicitante: {command_message.author_name}\n"
            f"Comando de criacao do card: {command_message.content}\n"
            f"Prazo identificado fora da LLM: {due_label}\n"
            f"Titulo heuristico atual: {requested_card.title}\n"
            f"Resumo heuristico atual: {requested_card.summary}\n"
            f"Detalhes heuristico atual: {requested_card.context_excerpt or 'nenhum'}\n\n"
            "Objetivo:\n"
            "- melhorar titulo, resumo e detalhes do card\n"
            "- manter fidelidade ao contexto\n"
            "- priorizar a demanda principal e seu desfecho esperado\n"
            "- usar os detalhes apenas para informacoes de apoio realmente uteis\n"
            "- nao mencionar 'mensagens', 'conversa', 'thread', horarios ou autores no resultado\n"
            "- nao inventar prazo, aprovadores, responsaveis ou decisoes\n\n"
            "Contexto selecionado do assunto:\n"
            f"{transcript}"
        )


def _build_system(today: date | None, label_names: list[str]) -> str:
    parts = [_SYSTEM_INSTRUCTIONS]
    if today is not None:
        parts.append(
            f"Data de hoje: {today.strftime('%d/%m/%Y')}. Se o pedido ou o contexto trouxer um prazo "
            "(inclusive relativo, como 'amanha' ou 'ate sexta'), converta para 'YYYY-MM-DD' em due_date. "
            "Nao invente prazo que nao esteja no texto."
        )
    if label_names:
        parts.append(
            "Tags disponiveis para o card: " + ", ".join(label_names) + ". "
            "Escolha em labels apenas as que claramente se aplicam; caso contrario, use lista vazia."
        )
    return "\n".join(parts)


def _build_schema(label_names: list[str]) -> dict:
    schema = copy.deepcopy(REQUESTED_CARD_SCHEMA)
    if label_names:
        schema["properties"]["labels"]["items"] = {"type": "string", "enum": label_names}
    return schema


def _parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _hash_author(author_id: str) -> str:
    return sha256(author_id.encode("utf-8")).hexdigest()


def _build_transcript(
    *,
    context_messages: list[DiscordMessage],
    command_message: DiscordMessage,
) -> str:
    lines: list[str] = []
    for message in context_messages:
        timestamp = message.timestamp.astimezone(command_message.timestamp.tzinfo).strftime("%d/%m %H:%M")
        body = _clean_text(message.content, limit=260)
        if not body:
            continue
        lines.append(f"- [{timestamp}] {message.author_name}: {body}")
    return "\n".join(lines) or "- Contexto indisponivel."


def _extract_output_text(response: dict) -> str:
    texts: list[str] = []
    for block in response.get("content", []):
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
    return "".join(texts).strip()


def _clean_text(text: str, *, limit: int) -> str:
    compact = " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())
    compact = " ".join(compact.split()).strip(" ,:-")
    if len(compact) > limit:
        compact = compact[: limit - 3].rstrip() + "..."
    return compact
