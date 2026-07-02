from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256

from .config import Settings
from .http import JsonApiClient
from .models import DiscordMessage


LOGGER = logging.getLogger(__name__)

MAX_THREAD_MESSAGES = 40
CARD_LINK_MARKER = "trello.com/c/"

DM_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reply", "card"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["ask", "confirm", "create"],
            "description": (
                "ask = falta informacao, faca uma pergunta objetiva. "
                "confirm = ja ha o minimo (objetivo + descricao); apresente um resumo e peca confirmacao. "
                "create = o usuario confirmou explicitamente; devolva os campos do card."
            ),
        },
        "reply": {
            "type": "string",
            "description": "Mensagem em portugues do Brasil para enviar ao usuario na DM.",
        },
        "card": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "description", "due_date"],
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Titulo objetivo do card. Vazio se action != create.",
                },
                "description": {
                    "type": "string",
                    "description": "Descricao do que precisa ser feito, so com o que o usuario disse. Vazio se action != create.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Prazo em 'YYYY-MM-DD' se o usuario informou; caso contrario vazio.",
                },
            },
        },
    },
}

_DEVELOPER_INSTRUCTIONS = (
    "Voce e o assistente do Infra Ops no Discord. Seu unico objetivo e transformar o pedido do "
    "usuario em um card do Trello, conversando por mensagem direta.\n"
    "Regras obrigatorias:\n"
    "- NUNCA invente informacoes. Use somente o que o usuario disse. Se faltar algo essencial, pergunte.\n"
    "- Se a mensagem for apenas uma saudacao ou conversa fiada (ex.: 'opa', 'oi', 'bom dia', 'tudo bem') "
    "sem nenhum pedido concreto, responda com action 'ask': cumprimente e pergunte no que pode ajudar. "
    "NUNCA crie card a partir de uma saudacao.\n"
    "- O minimo para criar um card e: um objetivo/titulo claro e uma descricao do que precisa ser feito. "
    "Prazo e opcional (pode perguntar uma vez, mas nao bloqueie a criacao por causa dele).\n"
    "- Enquanto faltar o minimo, responda com action 'ask' e faca UMA pergunta objetiva por vez.\n"
    "- Quando tiver o minimo e o usuario ainda NAO tiver confirmado, responda com action 'confirm': "
    "apresente um resumo curto (titulo, descricao e prazo se houver) e pergunte se pode criar o card.\n"
    "- Responda com action 'create' SOMENTE quando o usuario confirmar explicitamente "
    "(ex.: 'sim', 'pode criar', 'confirmo', 'isso'). Ai preencha card.title, card.description e "
    "card.due_date (ou vazio).\n"
    "- Se o usuario pedir alteracoes no resumo, volte para 'confirm' com o resumo ajustado.\n"
    "- Fale sempre em portugues do Brasil, tom cordial e direto. Nao cite horarios nem IDs."
)


@dataclass(frozen=True)
class DMCardDraft:
    title: str
    description: str
    due_date: date | None


@dataclass(frozen=True)
class DMDecision:
    action: str  # "ask" | "confirm" | "create"
    reply: str
    card: DMCardDraft | None


class DMConversationAssistant:
    """Conduz a conversa por DM ate entender a demanda e montar o card, via OpenAI."""

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY nao configurada.")
        self.settings = settings
        self.client = JsonApiClient(
            base_url=settings.openai_api_base_url,
            default_headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )

    def decide(self, thread_messages: list[DiscordMessage]) -> DMDecision:
        conversation = _current_request_thread(thread_messages)
        if not conversation:
            raise ValueError("Conversa de DM vazia.")

        author_id = next(
            (m.author_id for m in reversed(conversation) if not m.author_is_bot),
            conversation[-1].author_id,
        )
        payload = {
            "model": self.settings.openai_model,
            "reasoning": {"effort": self.settings.openai_reasoning_effort},
            "store": False,
            "safety_identifier": sha256(author_id.encode("utf-8")).hexdigest(),
            "input": [
                {"role": "developer", "content": _DEVELOPER_INSTRUCTIONS},
                *_conversation_to_input(conversation),
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "dm_card_decision",
                    "strict": True,
                    "schema": DM_DECISION_SCHEMA,
                }
            },
        }

        response = self.client.request("POST", "/responses", json_body=payload)
        output_text = _extract_output_text(response)
        if not output_text:
            raise ValueError("Resposta vazia da OpenAI na conversa de DM.")
        try:
            data = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ValueError("A OpenAI nao retornou JSON valido na conversa de DM.") from exc

        return _decision_from_data(data)


def _conversation_to_input(conversation: list[DiscordMessage]) -> list[dict]:
    items: list[dict] = []
    for message in conversation[-MAX_THREAD_MESSAGES:]:
        content = (message.content or "").strip()
        if not content:
            continue
        role = "assistant" if message.author_is_bot else "user"
        items.append({"role": role, "content": content})
    if not items or items[-1]["role"] != "user":
        # Garante que a ultima fala seja do usuario para a LLM responder.
        items.append({"role": "user", "content": conversation[-1].content.strip() or "(sem texto)"})
    return items


def _current_request_thread(thread_messages: list[DiscordMessage]) -> list[DiscordMessage]:
    """Retorna apenas as mensagens da solicitacao em andamento.

    Corta o historico logo apos a ultima mensagem do bot que ja entregou um card
    (contem um link de card do Trello), de forma que um card ja criado nao seja
    reaberto e cada nova demanda comece do zero.
    """
    ordered = sorted(thread_messages, key=lambda m: (m.timestamp, m.id))
    last_card_index = -1
    for index, message in enumerate(ordered):
        if message.author_is_bot and CARD_LINK_MARKER in (message.content or ""):
            last_card_index = index
    return ordered[last_card_index + 1:]


def _decision_from_data(data: dict) -> DMDecision:
    action = str(data.get("action") or "").strip().lower()
    if action not in {"ask", "confirm", "create"}:
        action = "ask"
    reply = str(data.get("reply") or "").strip()
    card_data = data.get("card") or {}
    card: DMCardDraft | None = None
    if action == "create":
        title = str(card_data.get("title") or "").strip()
        description = str(card_data.get("description") or "").strip()
        if not title:
            # Sem titulo nao ha card confiavel: volta a pedir confirmacao.
            return DMDecision(
                action="confirm",
                reply=reply or "Pode confirmar os detalhes para eu criar o card?",
                card=None,
            )
        card = DMCardDraft(
            title=title,
            description=description,
            due_date=_parse_iso_date(str(card_data.get("due_date") or "").strip()),
        )
    if not reply and action != "create":
        reply = "Pode me dar mais detalhes do que voce precisa?"
    return DMDecision(action=action, reply=reply, card=card)


def _parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_output_text(response: dict) -> str:
    texts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(str(content["text"]))
    return "".join(texts).strip()
