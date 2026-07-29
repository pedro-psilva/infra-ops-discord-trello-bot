from __future__ import annotations

import copy
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
    "Prazo e opcional: pergunte o prazo uma vez se o usuario nao informar, mas nao bloqueie a criacao por falta dele.\n"
    "- Se o usuario informar um prazo, mesmo relativo (ex.: 'amanha', 'ate sexta', 'dia 10', 'em 3 dias'), "
    "converta para o formato 'YYYY-MM-DD' e preencha card.due_date.\n"
    "- Escolha em card.labels apenas as tags que claramente se aplicam ao pedido, sempre dentre as tags "
    "disponiveis informadas. Se nenhuma se aplicar, use lista vazia.\n"
    "- Enquanto faltar o minimo, responda com action 'ask' e faca UMA pergunta objetiva por vez.\n"
    "- Quando tiver o minimo e o usuario ainda NAO tiver confirmado, responda com action 'confirm': "
    "apresente um resumo curto (titulo, descricao, prazo e tags quando houver) e pergunte se pode criar o card.\n"
    "- Responda com action 'create' SOMENTE quando o usuario confirmar explicitamente "
    "(ex.: 'sim', 'pode criar', 'confirmo', 'isso'). Ai preencha card.title, card.description, "
    "card.due_date e card.labels.\n"
    "- Se o usuario pedir alteracoes no resumo, volte para 'confirm' com o resumo ajustado.\n"
    "- Escreva sempre em português do Brasil correto, com acentuação e ortografia adequadas "
    "(por exemplo: 'você', 'não', 'está', 'até', 'atenção'), mesmo que o usuário escreva sem acentos. "
    "Use tom cordial e direto e não cite horários nem IDs."
)

_CHAT_INSTRUCTIONS = (
    "Você é o assistente do Infra Ops em um canal de grupo do Discord e só é acionado quando alguém "
    "te menciona com @. Responda de forma natural e útil a qualquer mensagem: tire dúvidas, converse, "
    "dê informações e ajude no que for pedido. Nem toda mensagem precisa virar um card do Trello.\n"
    "Regras obrigatórias:\n"
    "- NUNCA invente informações. Use somente o que foi dito na conversa.\n"
    "- Use action 'ask' para qualquer resposta normal: responder uma pergunta, conversar ou pedir mais "
    "detalhes. Coloque a resposta em 'reply' e deixe os campos do card vazios.\n"
    "- Só trate a mensagem como pedido de card quando a pessoa claramente pedir para criar/abrir uma "
    "tarefa, card ou demanda (ex.: 'abre um card', 'cria uma tarefa', 'preciso que registre isso').\n"
    "- Para criar um card o mínimo é: um objetivo/título claro e uma descrição do que precisa ser feito. "
    "Prazo é opcional: pergunte uma vez se não informarem, mas não bloqueie a criação por falta dele.\n"
    "- Se informarem um prazo, mesmo relativo (ex.: 'amanhã', 'até sexta', 'em 3 dias'), converta para "
    "'YYYY-MM-DD' e preencha card.due_date.\n"
    "- Em card.labels escolha apenas as tags que claramente se aplicam, dentre as disponíveis. Se nenhuma "
    "se aplicar, use lista vazia.\n"
    "- Quando tiver o mínimo para o card e a pessoa ainda NÃO tiver confirmado, use action 'confirm': "
    "apresente um resumo curto (título, descrição, prazo e tags quando houver) e pergunte se pode criar.\n"
    "- Responda com action 'create' SOMENTE quando a pessoa confirmar explicitamente "
    "(ex.: 'sim', 'pode criar', 'confirmo', 'isso'). Aí preencha card.title, card.description, "
    "card.due_date e card.labels.\n"
    "- Se pedirem alterações no resumo, volte para 'confirm' com o resumo ajustado.\n"
    "- Escreva sempre em português do Brasil correto, com acentuação e ortografia adequadas, mesmo que "
    "escrevam sem acentos. Use tom cordial e direto e não cite horários nem IDs."
)


@dataclass(frozen=True)
class DMCardDraft:
    title: str
    description: str
    due_date: date | None
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class DMDecision:
    action: str  # "ask" | "confirm" | "create"
    reply: str
    card: DMCardDraft | None


class DMConversationAssistant:
    """Conduz a conversa por DM ate entender a demanda e montar o card, via Anthropic."""

    def __init__(self, settings: Settings, *, instructions: str | None = None) -> None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY nao configurada.")
        self.settings = settings
        self._instructions = instructions or _DEVELOPER_INSTRUCTIONS
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

    def decide(
        self,
        thread_messages: list[DiscordMessage],
        *,
        today: date | None = None,
        available_labels: list[str] | None = None,
    ) -> DMDecision:
        conversation = _current_request_thread(thread_messages)
        if not conversation:
            raise ValueError("Conversa de DM vazia.")

        label_names = list(available_labels or [])
        author_id = next(
            (m.author_id for m in reversed(conversation) if not m.author_is_bot),
            conversation[-1].author_id,
        )
        payload = {
            "model": self.settings.anthropic_model,
            "max_tokens": 1024,
            "system": _build_system(self._instructions, today, label_names),
            "metadata": {"user_id": sha256(author_id.encode("utf-8")).hexdigest()},
            "messages": _conversation_to_input(conversation),
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": _build_decision_schema(label_names),
                }
            },
        }

        response = self.client.request("POST", "/messages", json_body=payload)
        output_text = _extract_output_text(response)
        if not output_text:
            raise ValueError("Resposta vazia da Anthropic na conversa de DM.")
        try:
            data = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ValueError("A Anthropic nao retornou JSON valido na conversa de DM.") from exc

        return _decision_from_data(data)


def build_chat_assistant(settings: Settings) -> DMConversationAssistant:
    return DMConversationAssistant(settings, instructions=_CHAT_INSTRUCTIONS)


def _build_system(instructions: str, today: date | None, label_names: list[str]) -> str:
    parts = [instructions]
    if today is not None:
        parts.append(
            f"Data de hoje: {today.strftime('%d/%m/%Y')}. Use esta data para converter "
            "qualquer prazo relativo em uma data no formato YYYY-MM-DD."
        )
    if label_names:
        parts.append("Tags disponiveis para o card: " + ", ".join(label_names) + ".")
    return "\n".join(parts)


def _build_decision_schema(label_names: list[str]) -> dict:
    schema = copy.deepcopy(DM_DECISION_SCHEMA)
    card = schema["properties"]["card"]
    items: dict = {"type": "string"}
    if label_names:
        items = {"type": "string", "enum": label_names}
    card["properties"]["labels"] = {
        "type": "array",
        "items": items,
        "description": "Nomes das tags aplicaveis, escolhidos apenas entre as tags disponiveis. Vazio se nenhuma se aplica.",
    }
    card["required"] = [*card["required"], "labels"]
    return schema


def _conversation_to_input(conversation: list[DiscordMessage]) -> list[dict]:
    items: list[dict] = []
    for message in conversation[-MAX_THREAD_MESSAGES:]:
        content = (message.content or "").strip()
        if not content:
            continue
        role = "assistant" if message.author_is_bot else "user"
        items.append({"role": role, "content": content})
    while items and items[0]["role"] != "user":
        items.pop(0)
    if not items or items[-1]["role"] != "user":
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
            labels=_parse_labels(card_data.get("labels")),
        )
    if not reply and action != "create":
        reply = "Pode me dar mais detalhes do que você precisa?"
    return DMDecision(action=action, reply=reply, card=card)


def _parse_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_output_text(response: dict) -> str:
    texts: list[str] = []
    for block in response.get("content", []):
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
    return "".join(texts).strip()
