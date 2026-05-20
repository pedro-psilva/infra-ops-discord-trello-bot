from __future__ import annotations

import re
from datetime import date, datetime

import dateparser
from dateparser.search import search_dates

from .config import Settings
from .models import DiscordMessage, ParseResult, ParsedTask, TaskType


NAME_WORD = r"[A-Za-zÀ-ÖØ-öø-ÿ'.-]+"
NAME_CONNECTORS = {"da", "das", "de", "do", "dos", "e"}

# Regex para _clean_name — compiladas uma vez
_CLEAN_NAME_DATE_SUFFIX_RE = re.compile(
    r"\s+[-–—]\s*(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{4}-\d{2}-\d{2})"
)
_CLEAN_NAME_LABEL_PREFIX_RE = re.compile(
    r"^(?:nome(?:\s+completo)?|colaborador(?:a)?|funcion[aá]ri[oa]|employee|pessoa|destinat[aá]rio)\s*[:\-]\s*",
    re.IGNORECASE,
)
_CLEAN_NAME_STOPWORD_SPLIT_RE = re.compile(
    r"(?:data|dia|endere[cç]o|logradouro|rua|avenida|bairro|cidade|cep|telefone|celular|e-?mail|cargo|[áa]rea|gestor|l[ií]der|modalidade|obs(?:erva[cç][aã]o)?|observa[cç][aã]o|quer|precisa|vai|retirar|buscar|devolver|uber|perif[eé]ricos?)",
    re.IGNORECASE,
)
_CLEAN_NAME_WHITESPACE_RE = re.compile(r"\s{2,}")
# Remove anotações parentéticas no final do nome, ex: "(é de BH)", "(Mercantil)"
_CLEAN_NAME_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")
# Separa nome do cargo/empresa após " - ", ex: "Jonathan Tavares - Tech Lead Bamaq"
_CLEAN_NAME_ROLE_SEPARATOR_RE = re.compile(r"\s+-\s+.*$")
# Detecta datas no cargo (usado para invalidar falsos positivos)
_CARGO_DATE_SENTINEL_RE = re.compile(r"\b\d{1,2}/\d{1,2}\b")

RAW_EXCERPT_MAX_LEN = 1800

# Cabeçalho de seção de data: "Data (07/07)", "**Data (11/05)**", "Data: 07/07" etc.
DATE_SECTION_HEADER_PATTERN = re.compile(
    r"(?:^|\n)\s*\*{0,2}\s*[Dd]ata\s*[:\-]?\s*[\(\[]?\s*"
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"\s*[\)\]]?\s*\*{0,2}\s*(?=\n|$)",
    re.MULTILINE,
)

ONBOARDING_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bonboardings?\b", re.IGNORECASE), 3),
    (re.compile(r"\bonboards?\b", re.IGNORECASE), 3),
    (re.compile(r"\badmiss[aã]o\b", re.IGNORECASE), 3),
    (re.compile(r"\bcontrata[cç][aã]o\b", re.IGNORECASE), 3),
    (re.compile(r"\bnovo(?:\s+\w+){0,2}\s+colaborador", re.IGNORECASE), 2),
    (re.compile(r"\b(?:vai\s+)?entrar\b", re.IGNORECASE), 1),
    (re.compile(r"\bcome[cç]a\b", re.IGNORECASE), 1),
)

OFFBOARDING_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\boffboardings?\b", re.IGNORECASE), 3),
    (re.compile(r"\boffboards?\b", re.IGNORECASE), 3),
    (re.compile(r"\bdesligamento\b", re.IGNORECASE), 3),
    (re.compile(r"\brescis[aã]o\b", re.IGNORECASE), 3),
    (re.compile(r"\b(?:vai\s+)?sair\b", re.IGNORECASE), 1),
    (re.compile(r"\bsa[ií]da\b", re.IGNORECASE), 1),
    (re.compile(r"\b[úu]ltimo dia\b", re.IGNORECASE), 1),
)

LABEL_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:nome(?:\s+completo)?|colaborador(?:a)?|funcion[aá]ri[oa]|employee|pessoa|destinat[aá]rio)\s*[:\-]\s*(?P<name>[^\n;|]+)",
        re.IGNORECASE,
    ),
)

GREETING_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"^\s*(?:bom\s+dia|boa\s+tarde|boa\s+noite),\s*(?P<name>{NAME_WORD}(?:\s+{NAME_WORD}){{0,3}})\s*(?:[!,]|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
)

ONBOARDING_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:onboardings?|onboards?|admiss[aã]o|contrata[cç][aã]o|entrada)\s*(?:de|da|do)?[ \t]*[:\-]?[ \t]*(?P<name>[^\n;|]+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<name>{NAME_WORD}(?:\s+{NAME_WORD}){{0,9}})\s+(?:vai\s+)?(?:entrar|come[cç]ar|iniciar)",
        re.IGNORECASE,
    ),
    # Formato "Onboarding DD/MM * Nome - Cargo (Local)" — asterisco como separador inline
    re.compile(
        rf"\*\s+(?P<name>{NAME_WORD}(?:\s+{NAME_WORD}){{1,9}})(?=\s*(?:-|\(|$))",
        re.IGNORECASE,
    ),
)

OFFBOARDING_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:offboardings?|offboards?|desligamento|sa[ií]da|rescis[aã]o)\s*(?:de|da|do)?[ \t]*[:\-]?[ \t]*(?P<name>[^\n;|]+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<name>{NAME_WORD}(?:\s+{NAME_WORD}){{0,9}})\s+(?:vai\s+)?(?:sair|ser\s+desligad[oa])",
        re.IGNORECASE,
    ),
    # Formato "Offboarding DD/MM * Nome - Cargo (Local)" — asterisco como separador inline
    re.compile(
        rf"\*\s+(?P<name>{NAME_WORD}(?:\s+{NAME_WORD}){{1,9}})(?=\s*(?:-|\(|$))",
        re.IGNORECASE,
    ),
)

LIST_BULLET_PATTERN = re.compile(r"^\s*[-*•]\s*(?P<name>[^\n:]+?)\s*$", re.MULTILINE)

DATE_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"\b\d{1,2}-\d{1,2}(?:-\d{2,4})?\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b\d{1,2}\s+de\s+(?:jan(?:eiro)?|fev(?:ereiro)?|mar(?:[çc]o)?|abr(?:il)?|maio|jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|set(?:embro)?|out(?:ubro)?|nov(?:embro)?|dez(?:embro)?)\b",
        re.IGNORECASE,
    ),
)

NOTE_KEYWORDS: tuple[str, ...] = (
    "obs",
    "observa",
    "endereco",
    "endereço",
    "logradouro",
    "rua",
    "avenida",
    "bairro",
    "cidade",
    "cep",
    "numero",
    "número",
    "complemento",
    "telefone",
    "celular",
    "email",
    "e-mail",
    "cargo",
    "area",
    "área",
    "gestor",
    "lider",
    "líder",
    "modalidade",
    "perif",
    "monitor",
    "mouse",
    "teclado",
    "headset",
    "fone",
    "notebook",
    "retirar",
    "retira",
    "buscar",
    "busca",
    "devolver",
    "devolucao",
    "devolução",
    "ultimo dia",
    "último dia",
    "uber",
    "coleta",
    "entrega",
)

NAME_STOPWORDS = {
    "onboarding",
    "offboarding",
    "onboardings",
    "offboardings",
    "admissao",
    "admissão",
    "desligamento",
    "saida",
    "saída",
    "data",
    "dia",
    "obs",
    "observacao",
    "observação",
    "perifericos",
    "periféricos",
    "proximos",
    "próximos",
}


class TaskParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse_message(self, message: DiscordMessage) -> ParseResult:
        text = _normalize_text(message.content)
        if not text:
            return ParseResult(reason="mensagem sem conteudo")

        task_type = _detect_task_type(text)
        if task_type is None:
            return ParseResult(reason="tipo de tarefa nao identificado")

        relative_base = message.timestamp.astimezone(self.settings.timezone)

        # Tenta parsing por seções de data (ex: "Data (07/07)" ... "Data (11/05)" ...)
        sections = _split_into_date_sections(text, relative_base=relative_base)
        if sections:
            all_tasks: list[ParsedTask] = []
            for section_date, section_text in sections:
                names = _extract_employee_names(text=section_text, task_type=task_type)
                notes = tuple(_extract_notes(section_text))
                for name in names:
                    cargo = _extract_cargo_for_name(section_text, name)
                    all_tasks.append(
                        ParsedTask(
                            task_type=task_type,
                            employee_name=name,
                            effective_date=section_date,
                            notes=notes,
                            raw_excerpt=section_text[:RAW_EXCERPT_MAX_LEN],
                            cargo=cargo,
                        )
                    )
            if all_tasks:
                return ParseResult(tasks=tuple(all_tasks))

        # Fallback: parsing de data única
        effective_date = _extract_date(
            text=text,
            relative_base=relative_base,
            timezone_name=str(self.settings.timezone),
        )
        if effective_date is None:
            return ParseResult(reason="data nao identificada")

        employee_names = _extract_employee_names(text=text, task_type=task_type)
        if not employee_names:
            return ParseResult(reason="nome do colaborador nao identificado")

        notes = tuple(_extract_notes(text))
        raw_excerpt = text[:RAW_EXCERPT_MAX_LEN]
        return ParseResult(
            tasks=tuple(
                ParsedTask(
                    task_type=task_type,
                    employee_name=employee_name,
                    effective_date=effective_date,
                    notes=notes,
                    raw_excerpt=raw_excerpt,
                    cargo=_extract_cargo_for_name(text, employee_name),
                )
                for employee_name in employee_names
            )
        )


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _score_patterns(text: str, patterns: tuple[tuple[re.Pattern[str], int], ...]) -> int:
    return sum(weight for pattern, weight in patterns if pattern.search(text))


def _detect_task_type(text: str) -> TaskType | None:
    onboarding_score = _score_patterns(text, ONBOARDING_PATTERNS)
    offboarding_score = _score_patterns(text, OFFBOARDING_PATTERNS)

    if onboarding_score == 0 and offboarding_score == 0:
        return None
    if onboarding_score == offboarding_score:
        return None
    return TaskType.ONBOARDING if onboarding_score > offboarding_score else TaskType.OFFBOARDING


def _extract_date(text: str, relative_base: datetime, timezone_name: str) -> date | None:
    settings = {
        "RELATIVE_BASE": relative_base,
        "DATE_ORDER": "DMY",
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DATES_FROM": "future",
    }

    candidates: list[tuple[int, date]] = []

    for pattern in DATE_TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            parsed_date = _parse_explicit_date_token(match.group(0), default_year=relative_base.year)
            if parsed_date is None:
                parsed = dateparser.parse(match.group(0), languages=["pt", "en"], settings=settings)
                if parsed is None:
                    continue
                parsed_date = parsed.date()
            candidates.append((match.start(), parsed_date))

    if not candidates:
        searched = search_dates(text, languages=["pt", "en"], settings=settings) or []
        for fragment, parsed in searched:
            if not _looks_like_date_fragment(fragment):
                continue
            position = text.find(fragment)
            if position == -1:
                position = len(text)
            candidates.append((position, parsed.date()))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _parse_explicit_date_token(token: str, *, default_year: int) -> date | None:
    compact = token.strip()
    match = re.fullmatch(r"(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{2,4}))?", compact)
    if not match:
        return None
    year = match.group("year")
    if year is None:
        parsed_year = default_year
    elif len(year) == 2:
        parsed_year = 2000 + int(year)
    else:
        parsed_year = int(year)
    try:
        return date(parsed_year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


def _looks_like_date_fragment(fragment: str) -> bool:
    if re.search(r"\d", fragment):
        return True
    month_keywords = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
    lowered = fragment.lower()
    relative_keywords = ("hoje", "amanha", "amanhã")
    return any(keyword in lowered for keyword in month_keywords + relative_keywords)


def _extract_employee_names(text: str, task_type: TaskType) -> tuple[str, ...]:
    patterns = list(LABEL_NAME_PATTERNS)
    patterns.extend(ONBOARDING_NAME_PATTERNS if task_type is TaskType.ONBOARDING else OFFBOARDING_NAME_PATTERNS)

    for pattern in patterns:
        for match in pattern.finditer(text):
            candidate = _clean_name(match.group("name"))
            if candidate:
                return (candidate,)

    if task_type is TaskType.OFFBOARDING:
        for pattern in GREETING_NAME_PATTERNS:
            for match in pattern.finditer(text):
                candidate = _clean_greeting_name(match.group("name"))
                if candidate:
                    return (candidate,)

    list_candidates = _extract_list_names(text)
    if list_candidates:
        return tuple(list_candidates)

    return ()


def _extract_list_names(text: str) -> list[str]:
    names: list[str] = []

    for match in LIST_BULLET_PATTERN.finditer(text):
        for candidate in _split_candidate_names(match.group("name")):
            if candidate not in names:
                names.append(candidate)
    if names:
        return names

    lines = [line.strip() for line in text.splitlines()]
    for line in lines[1:]:
        cleaned = line.strip(" ,-*•\t")
        if not cleaned:
            continue
        if ":" in cleaned:
            continue
        lowered = cleaned.lower()
        if any(keyword in lowered for keyword in NOTE_KEYWORDS):
            break

        candidates = _split_candidate_names(cleaned)
        if candidates:
            for candidate in candidates:
                if candidate not in names:
                    names.append(candidate)
        elif names:
            break

    return names


def _split_candidate_names(raw: str) -> list[str]:
    cleaned = raw.strip(" ,")
    pieces = [piece.strip() for piece in re.split(r"\s*,\s*|\s+e\s+", cleaned) if piece.strip()]
    if not pieces:
        return []

    candidates = [_clean_name(piece) for piece in pieces]
    valid_candidates = [candidate for candidate in candidates if candidate]
    if valid_candidates and len(valid_candidates) == len(pieces):
        return valid_candidates

    whole_candidate = _clean_name(cleaned)
    if whole_candidate:
        return [whole_candidate]

    return []


def _looks_like_name(candidate: str) -> bool:
    words = [word for word in candidate.split() if word]
    if len(words) < 2 or len(words) > 10:
        return False

    has_real_name_word = False
    for word in words:
        lowered = word.lower()
        if lowered in NAME_CONNECTORS:
            continue
        if lowered in NAME_STOPWORDS:
            return False
        if not word[0].isalpha() or not word[0].isupper():
            return False
        has_real_name_word = True

    return has_real_name_word


def _clean_name(raw: str) -> str | None:
    name = raw.strip(" .,-:;|/!?\t")
    name = name.strip("*_`")
    name = re.sub(r"\s*<[^>]+>\s*$", "", name).strip()
    name = _CLEAN_NAME_PARENTHETICAL_RE.sub("", name).strip()
    name = _CLEAN_NAME_ROLE_SEPARATOR_RE.sub("", name).strip()
    name = _CLEAN_NAME_DATE_SUFFIX_RE.split(name, maxsplit=1)[0]
    name = _CLEAN_NAME_LABEL_PREFIX_RE.sub("", name)
    name = _CLEAN_NAME_STOPWORD_SPLIT_RE.split(name, maxsplit=1)[0]
    name = name.strip(" .,-:;|/!?\t")
    name = _CLEAN_NAME_WHITESPACE_RE.sub(" ", name)

    if not name:
        return None
    if any(char.isdigit() for char in name):
        return None
    if name.lower() in NAME_STOPWORDS:
        return None
    if name.startswith("<@") and name.endswith(">"):
        return None
    if not _looks_like_name(name):
        return None

    return name


def _clean_greeting_name(raw: str) -> str | None:
    name = raw.strip(" .,!?:;|/\t")
    name = _CLEAN_NAME_WHITESPACE_RE.sub(" ", name)
    if not name or any(char.isdigit() for char in name):
        return None

    words = name.split()
    if len(words) > 4:
        return None
    for word in words:
        lowered = word.lower()
        if lowered in NAME_CONNECTORS:
            continue
        if lowered in NAME_STOPWORDS:
            return None
        if not word[0].isalpha() or not word[0].isupper():
            return None

    return name


def _extract_cargo_for_name(text: str, employee_name: str) -> str | None:
    """Extrai o cargo do colaborador quando o texto tem formato 'Nome - Cargo (Local)'.

    Retorna o cargo como string limpa, ou None se não identificado.
    """
    escaped = re.escape(employee_name)
    match = re.search(
        escaped + r"\s*-\s*([^(\n*]{2,60})(?=\s*\(|\s*\*|\s*$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    cargo = match.group(1).strip(" .,;*_`\n\t")
    if not cargo:
        return None
    # Invalida se parece com data (ex: "20/05")
    if _CARGO_DATE_SENTINEL_RE.search(cargo):
        return None
    # Cargo não deve ter mais de 8 palavras
    if len(cargo.split()) > 8:
        return None
    return cargo


def _split_into_date_sections(
    text: str, *, relative_base: datetime
) -> list[tuple[date, str]] | None:
    """Divide o texto em seções por data quando há múltiplos cabeçalhos 'Data (dd/mm)'.

    Retorna uma lista de (data, texto_da_seção) ou None se houver menos de 2 seções.
    """
    matches = list(DATE_SECTION_HEADER_PATTERN.finditer(text))
    if len(matches) < 2:
        return None

    sections: list[tuple[date, str]] = []
    for i, match in enumerate(matches):
        date_token = match.group(1)
        section_start = match.end()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[section_start:section_end].strip()
        parsed_date = _parse_explicit_date_token(date_token, default_year=relative_base.year)
        if parsed_date is None:
            continue
        sections.append((parsed_date, section_text))

    return sections if len(sections) >= 2 else None


def _extract_notes(text: str) -> list[str]:
    notes: list[str] = []
    for line in re.split(r"\n+", text):
        cleaned_line = line.strip(" -*\t")
        if not cleaned_line:
            continue

        lowered = cleaned_line.lower()
        if any(keyword in lowered for keyword in NOTE_KEYWORDS):
            if cleaned_line not in notes:
                notes.append(cleaned_line)

    return notes
