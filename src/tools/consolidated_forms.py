"""
Consolidated MCP tools for the native GLPI 11 Forms module (service catalog).

Two tools cover the whole form surface:

  1. glpi_search_forms  -- list / text-search forms and catalog categories
  2. glpi_manage_forms  -- get/create/update/delete a form, its sections,
                           questions, comments and catalog categories

Why one module and not five
---------------------------
A tool per itemtype (form, section, question, comment, category) would produce
five near-identical schemas. One `action` argument keeps the catalogue small
and the behaviour identical, which is what makes "build a service catalog"
a sequence of calls on the same tool instead of a guessing game between five.

All rendering lives here on purpose: this module owns its Markdown so it can
ship without touching the shared formatters.
"""

import json
from typing import Any, Dict, List, Optional

from src.formatters.markdown_helpers import (
    esc,
    fmt_date,
    page_info,
    strip_html,
    truncate_field,
)
from src.models.exceptions import GLPIError, NotFoundError, ValidationError
from src.services.form_service import (
    DESTINATION_URGENCY_FIELD_KEY,
    RENDER_LAYOUT_LABELS,
    form_service,
    resolve_question_type,
    resolve_render_layout,
)
from src.utils.helpers import (
    PaginationHelper,
    entity_resolver,
    input_sanitizer,
    logger,
)
from src.utils.safety_guard import require_safety_confirmation
from src.utils.validators import create_mcp_error, validate_positive_int

MAX_LIMIT = 50

MANAGE_ACTIONS = [
    # form
    "get", "create", "update", "delete",
    # sections
    "list_sections", "get_section", "create_section", "update_section", "delete_section",
    # questions
    "get_question", "create_question", "update_question", "delete_question",
    # comments
    "get_comment", "create_comment", "update_comment", "delete_comment",
    # catalog categories
    "get_category", "create_category", "update_category", "delete_category",
    # destinations (aba "Chamado")
    "list_destinations", "get_destination", "update_destination",
]

ACTIONS_REQUIRING_FORM_ID = [
    "get", "update", "delete", "list_sections", "create_section", "list_destinations",
]

#: Safety-guard operation name per delete action.
_DELETE_GUARD = {
    "delete": ("delete_form", "Form"),
    "delete_section": ("delete_form_section", "Section"),
    "delete_question": ("delete_form_question", "Question"),
    "delete_comment": ("delete_form_comment", "Comment"),
    "delete_category": ("delete_form_category", "Category"),
}


def _coerce_int(value: Any) -> Any:
    """Best-effort int coercion for callers that ignore the JSON Schema type."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return value


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "sim", "on")


async def _resolve_entity(entity_name: str) -> int:
    """Resolve an entity name to its id, listing the alternatives on failure."""
    resolved_id = await entity_resolver.resolve_entity_name(entity_name)
    if resolved_id is not None:
        return resolved_id
    available = await entity_resolver.list_available_entities()
    raise ValidationError(
        f"Entidade '{entity_name}' nao encontrada. Disponiveis: "
        f"{[e['name'] for e in available[:10]]}",
        "entity_name",
    )


def _require_id(value: Any, label: str) -> int:
    check = validate_positive_int(value, label)
    if not check["valid"]:
        raise ValidationError(check["error"], label)
    return check["value"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ACTIVE = {1: "Sim", "1": "Sim", "True": "Sim", "true": "Sim", 0: "Nao", "0": "Nao", "False": "Nao", "false": "Nao", "": "Nao", None: "N/A"}


def _fmt_bool(value: Any) -> str:
    return _ACTIVE.get(value, "Nao")


def _fmt_question_type(raw_type: Any) -> str:
    text = str(raw_type or "")
    if not text:
        return "N/A"
    short = text.rsplit("\\", 1)[-1]
    return short.replace("QuestionType", "")


def _fmt_render_layout(raw_layout: Any) -> str:
    text = str(raw_layout or "").strip().lower()
    if not text:
        return "N/A"
    return RENDER_LAYOUT_LABELS.get(text, text)


def _fmt_rows(scope: str, payload: Dict[str, Any], args: dict) -> str:
    items = payload.get("items") or []
    if not items:
        noun = "formularios" if scope == "forms" else "categorias do catalogo"
        return f"Nenhum {noun} encontrado."

    limit = int(payload.get("limit") or args.get("limit") or 10)
    offset = int(payload.get("offset") or args.get("offset") or 0)
    header = page_info(len(items), limit, offset, payload.get("total"))

    if scope == "forms":
        columns = ("id", "name", "category", "is_active", "date_mod")
        headers = ("ID", "Titulo", "Categoria", "Ativo", "Atualizado")
        rows = []
        for item in items:
            rows.append(
                "| {} | {} | {} | {} | {} |".format(
                    esc(item.get("id")),
                    esc(truncate_field(item.get("name"), 150) or "—"),
                    esc(truncate_field(item.get("category"), 80) or "—"),
                    _fmt_bool(item.get("is_active")),
                    fmt_date(item.get("date_mod")),
                )
            )
    else:
        columns = ("id", "name", "description", "entity")
        headers = ("ID", "Nome", "Descricao", "Entidade")
        rows = []
        for item in items:
            rows.append(
                "| {} | {} | {} | {} |".format(
                    esc(item.get("id")),
                    esc(truncate_field(item.get("name"), 120) or "—"),
                    esc(truncate_field(strip_html(item.get("description")), 120) or "—"),
                    esc(truncate_field(item.get("entity"), 80) or "—"),
                )
            )

    sep = "|".join("---" for _ in columns)
    return (
        f"{header}\n\n"
        f"| {' | '.join(headers)} |\n"
        f"|{sep}|\n" + "\n".join(rows)
    )


def _format_form_detail(item: Dict[str, Any]) -> str:
    if not item:
        return "Formulario nao encontrado."
    title = strip_html(str(item.get("name") or "")).strip() or "(sem titulo)"
    lines = [
        f"# Formulario {esc(item.get('id'))} — {esc(title)}",
        "",
        f"- **Categoria:** {esc(item.get('forms_categories_id') or item.get('category') or 'N/A')}",
        f"- **Layout:** {esc(_fmt_render_layout(item.get('render_layout')))}",
        f"- **Ativo:** {_fmt_bool(item.get('is_active'))}",
        f"- **Fixado no topo:** {_fmt_bool(item.get('is_pinned'))}",
        f"- **Rascunho:** {_fmt_bool(item.get('is_draft'))}",
        f"- **Entidade:** {esc(item.get('entities_id') or 'N/A')}",
        f"- **Criado:** {fmt_date(item.get('date_creation'))}",
        f"- **Atualizado:** {fmt_date(item.get('date_mod'))}",
    ]

    description = item.get("description")
    if description:
        lines.append("")
        lines.append("## Descricao")
        lines.append("")
        lines.append(truncate_field(strip_html(str(description)), 3000) or "N/A")

    sections = item.get("sections")
    if sections:
        lines.append("")
        lines.append(f"## Secoes ({len(sections)})")
        for index, section in enumerate(sections, start=1):
            if not isinstance(section, dict):
                continue
            section_name = strip_html(str(section.get("name") or "")).strip() or "(sem titulo)"
            lines.append("")
            lines.append(f"### {index}. {esc(section_name)} (ID {section.get('id')})")
            description = section.get("description")
            if description:
                lines.append("")
                lines.append(truncate_field(strip_html(str(description)), 800) or "")
            questions = section.get("questions") or []
            for q in questions:
                if not isinstance(q, dict):
                    continue
                q_name = strip_html(str(q.get("name") or "")).strip() or "(sem titulo)"
                flags = []
                if _fmt_bool(q.get("is_mandatory")) == "Sim":
                    flags.append("obrigatoria")
                conds = _parse_conditions(q.get("conditions"))
                if conds:
                    flags.append(f"{len(conds)} condicao(oes) de visibilidade")
                suffix = f", {', '.join(flags)}" if flags else ""
                lines.append(
                    f"- **Pergunta:** {esc(q_name)} "
                    f"(_tipo:_ {_fmt_question_type(q.get('type'))}, ID {q.get('id')}, "
                    f"UUID `{q.get('uuid') or '?'}`{suffix})"
                )
            comments = section.get("comments") or []
            for c in comments:
                if not isinstance(c, dict):
                    continue
                c_text = strip_html(str(c.get("description") or "")).strip() or (
                    strip_html(str(c.get("name") or "")).strip()
                )
                lines.append(f"- **Comentario:** {esc(truncate_field(c_text, 200) or '—')}")

    return "\n".join(lines)


def _format_simple_detail(label: str, item: Dict[str, Any]) -> str:
    if not item:
        return f"{label} nao encontrado."
    title = strip_html(str(item.get("name") or "")).strip() or "(sem titulo)"
    lines = [f"# {label} {esc(item.get('id'))} — {esc(title)}"]
    for key, field_label in (
        ("uuid", "UUID"),
        ("description", "Descricao"),
        ("forms_sections_id", "Secao"),
        ("forms_forms_id", "Formulario"),
        ("forms_categories_id", "Categoria pai"),
        ("type", "Tipo"),
        ("rank", "Ordem"),
        ("vertical_rank", "Linha"),
        ("horizontal_rank", "Coluna"),
        ("is_mandatory", "Obrigatoria"),
        ("default_value", "Valor padrao"),
        ("extra_data", "Configuracao"),
    ):
        if key not in item or item[key] in (None, ""):
            continue
        value = item[key]
        if isinstance(value, (dict, list)):
            value = str(value)
        value = str(value)
        if key == "type":
            value = _fmt_question_type(value)
        elif key == "is_mandatory":
            value = _fmt_bool(value)
        lines.append(f"- **{field_label}:** {esc(truncate_field(strip_html(value), 500) or '—')}")

    for cond_key, cond_label in (
        ("conditions", "Condicoes de visibilidade"),
        ("validation_conditions", "Condicoes de validacao"),
    ):
        raw = item.get(cond_key)
        if not raw:
            continue
        conds = _parse_conditions(raw)
        if not conds:
            lines.append(f"- **{cond_label}:** —")
        else:
            lines.append(f"- **{cond_label}:** {len(conds)} regra(s)")
            for index, cond in enumerate(conds, start=1):
                if not isinstance(cond, dict):
                    continue
                lines.append(
                    f"  {index}. item_uuid=`{cond.get('item_uuid') or cond.get('question_uuid') or '?'}` "
                    f"item_type=`{cond.get('item_type') or '?'}` "
                    f"operador=`{cond.get('value_operator') or '?'}` "
                    f"valor=`{cond.get('value') or ''}`"
                )
    return "\n".join(lines)


def _parse_conditions(raw: Any) -> List[Dict[str, Any]]:
    """Parse the conditions JSON column into a list of condition dicts."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    return []


def _format_destination_detail(item: Dict[str, Any]) -> str:
    if not item:
        return "Destino nao encontrado."
    title = strip_html(str(item.get("name") or "")).strip() or "(sem titulo)"
    lines = [
        f"# Destino {esc(item.get('id'))} — {esc(title)}",
        "",
        f"- **Formulario:** {esc(item.get('forms_forms_id') or 'N/A')}",
        f"- **Tipo:** {esc((item.get('itemtype') or '').rsplit('\\\\', 1)[-1] or 'N/A')}",
    ]
    config = item.get("config") or {}
    if isinstance(config, str):
        config = _parse_conditions(config)  # tolerant: may be a dict
        if isinstance(config, dict):
            config = config
        elif isinstance(config, list) and not config:
            config = {}
    urgency = config.get(DESTINATION_URGENCY_FIELD_KEY) if isinstance(config, dict) else None
    if urgency:
        lines.append(f"- **Urgencia do chamado:** resposta da pergunta `{urgency.get('specific_question_id')}` (estrategia `{urgency.get('strategy')}`)")
    if isinstance(config, dict) and len(config) > 1:
        lines.append(f"- **Outras configs de campo:** {', '.join(sorted(config))}")
    lines.append(f"- **Configuracao bruta:** {esc(json.dumps(config, ensure_ascii=False)[:2000])}")
    return "\n".join(lines)


def _format_mutation(action: str, result: Dict[str, Any]) -> str:
    resource_id = result.get("id")
    if action.startswith("delete"):
        mode = "purgado (removido definitivamente)" if result.get("purged") else "movido para a lixeira"
        return f"Registro {resource_id} {mode}."
    return f"Registro {resource_id} salvo com sucesso."


# ---------------------------------------------------------------------------
# Tool 1: glpi_search_forms
# ---------------------------------------------------------------------------


async def search_forms(
    scope: str = "forms",
    query: Optional[str] = None,
    is_active: Optional[bool] = None,
    category_id: Optional[int] = None,
    entity_id: Optional[int] = None,
    entity_name: Optional[str] = None,
    sort_by: Optional[str] = None,
    order: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
) -> Any:
    """Search / list forms or service-catalog categories."""
    try:
        if scope not in ("forms", "categories"):
            return create_mcp_error(
                f"scope '{scope}' desconhecido",
                "Esperado um de: forms, categories",
                "Exemplo: glpi_search_forms(scope='forms', query='computador')",
            )

        entity_id = _coerce_int(entity_id)
        category_id = _coerce_int(category_id)
        offset, limit = PaginationHelper.validate_pagination_params(offset, limit)
        limit = min(int(limit), MAX_LIMIT)

        if entity_name:
            entity_id = await _resolve_entity(entity_name)

        if query is not None:
            query = input_sanitizer.sanitize_search_query(query)
            if not query or len(query) < 2:
                raise ValidationError(
                    "A busca textual precisa de pelo menos 2 caracteres", "query"
                )

        logger.info(
            f"search_forms: scope={scope}, query={query}, limit={limit}, offset={offset}"
        )

        if scope == "forms":
            payload = await form_service.search_forms(
                query=query,
                entity_id=entity_id,
                is_active=is_active,
                category_id=category_id,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                order=order,
            )
        else:
            payload = await form_service.list_categories(
                query=query,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                order=order,
            )
        return _fmt_rows(scope, payload, {"limit": limit, "offset": offset})

    except (ValidationError, GLPIError) as exc:
        logger.error(f"search_forms error: {exc.message}")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(f"search_forms unexpected error: {exc}")
        raise GLPIError(500, f"Falha ao buscar formularios: {exc}")


# ---------------------------------------------------------------------------
# Tool 2: glpi_manage_forms
# ---------------------------------------------------------------------------


async def manage_forms(
    action: str,
    form_id: Optional[int] = None,
    section_id: Optional[int] = None,
    question_id: Optional[int] = None,
    comment_id: Optional[int] = None,
    category_id: Optional[int] = None,
    destination_id: Optional[int] = None,
    # form / section / question / comment / category content
    name: Optional[str] = None,
    description: Optional[str] = None,
    header: Optional[str] = None,
    render_layout: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_pinned: Optional[bool] = None,
    is_draft: Optional[bool] = None,
    rank: Optional[int] = None,
    vertical_rank: Optional[int] = None,
    horizontal_rank: Optional[int] = None,
    parent_id: Optional[int] = None,
    entity_id: Optional[int] = None,
    entity_name: Optional[str] = None,
    # question specifics
    type: Optional[str] = None,
    is_mandatory: Optional[bool] = None,
    default_value: Any = None,
    extra_data: Optional[Dict[str, Any]] = None,
    options: Optional[List[str]] = None,
    is_multiple_dropdown: Optional[bool] = None,
    conditions: Optional[List[Dict[str, Any]]] = None,
    validation_conditions: Optional[List[Dict[str, Any]]] = None,
    # destination specifics (aba "Chamado")
    config: Optional[Dict[str, Any]] = None,
    urgency_question_id: Optional[int] = None,
    urgency_strategy: Optional[str] = None,
    # create-form defaults
    init_sections: bool = True,
    init_destinations: bool = True,
    init_access_policies: bool = True,
    # delete safety
    purge: bool = True,
    confirmation_token: Optional[str] = None,
    reason: Optional[str] = None,
) -> Any:
    """Read and mutate one form, section, question, comment, category or destination."""
    try:
        action = str(action or "").strip()
        if action not in MANAGE_ACTIONS:
            return create_mcp_error(
                f"Acao '{action}' desconhecida",
                f"Esperado um de: {MANAGE_ACTIONS}",
                "Exemplo: glpi_manage_forms(action='create', name='Acesso VPN', "
                "description='Solicitar acesso VPN')",
            )

        form_id = _coerce_int(form_id)
        section_id = _coerce_int(section_id)
        question_id = _coerce_int(question_id)
        comment_id = _coerce_int(comment_id)
        category_id = _coerce_int(category_id)
        destination_id = _coerce_int(destination_id)
        rank = _coerce_int(rank)
        vertical_rank = _coerce_int(vertical_rank)
        horizontal_rank = _coerce_int(horizontal_rank)
        parent_id = _coerce_int(parent_id)
        entity_id = _coerce_int(entity_id)
        urgency_question_id = _coerce_int(urgency_question_id)
        # @MX:WARN: is_active/is_pinned/is_draft sao preservados crus aqui.
        # @MX:REASON: _coerce_bool(None) -> False. Coagir no topo tornava o
        # default "None significa nao informado" impossivel: no create, is_active
        # virava False em vez do default True; no update, um campo nao enviado
        # era gravado como 0. A coercao acontece por ramo, onde o default de cada
        # acao e conhecido.
        is_multiple_dropdown = (
            None if is_multiple_dropdown is None else _coerce_bool(is_multiple_dropdown)
        )
        is_mandatory = (
            None if is_mandatory is None else _coerce_bool(is_mandatory)
        )
        init_sections = _coerce_bool(init_sections)
        init_destinations = _coerce_bool(init_destinations)
        init_access_policies = _coerce_bool(init_access_policies)
        purge = _coerce_bool(purge)

        if entity_name and action in ("create", "update"):
            entity_id = await _resolve_entity(entity_name)

        # ---- delete safety guard ---------------------------------------
        guard = _DELETE_GUARD.get(action)
        if guard:
            op, target_type = guard
            target_id = {
                "delete": form_id,
                "delete_section": section_id,
                "delete_question": question_id,
                "delete_comment": comment_id,
                "delete_category": category_id,
            }[action]
            require_safety_confirmation(
                op,
                confirmation_token=confirmation_token,
                reason=reason,
                target_id=target_id,
                target_type=target_type,
            )

        # ---- read actions ----------------------------------------------
        if action == "get":
            item = await form_service.get_form(_require_id(form_id, "form_id"))
            return _format_form_detail(item)
        if action == "list_sections":
            sections = await form_service.list_sections(_require_id(form_id, "form_id"))
            if not sections:
                return "Nenhuma secao encontrada no formulario."
            lines = [f"**{len(sections)} secao(oes)** no formulario {form_id}:"]
            for index, section in enumerate(sections, start=1):
                s_name = strip_html(str(section.get("name") or "")).strip() or "(sem titulo)"
                lines.append(f"{index}. {esc(s_name)} (ID {section.get('id')})")
            return "\n".join(lines)
        if action == "get_section":
            return _format_simple_detail(
                "Secao", await form_service.get_section(
                    _require_id(section_id, "section_id"))
            )
        if action == "get_question":
            return _format_simple_detail(
                "Pergunta", await form_service.get_question(
                    _require_id(question_id, "question_id"))
            )
        if action == "get_comment":
            return _format_simple_detail(
                "Comentario", await form_service.get_comment(
                    _require_id(comment_id, "comment_id"))
            )
        if action == "get_category":
            return _format_simple_detail(
                "Categoria", await form_service.get_category(
                    _require_id(category_id, "category_id"))
            )

        # ---- form writes ------------------------------------------------
        if action == "create":
            if not name:
                return create_mcp_error(
                    "'name' e obrigatorio para criar um formulario",
                    "Forneca o titulo do formulario",
                    "Exemplo: glpi_manage_forms(action='create', name='Acesso VPN')",
                )
            item = await form_service.create_form(
                name=name,
                description=description,
                header=header,
                render_layout=resolve_render_layout(render_layout),
                category_id=category_id,
                entity_id=entity_id,
                is_active=True if is_active is None else _coerce_bool(is_active),
                is_pinned=_coerce_bool(is_pinned),
                is_draft=_coerce_bool(is_draft),
                init_sections=init_sections,
                init_destinations=init_destinations,
                init_access_policies=init_access_policies,
            )
            return _format_form_detail(item)

        if action == "update":
            item = await form_service.update_form(
                _require_id(form_id, "form_id"),
                name=name,
                description=description,
                header=header,
                render_layout=resolve_render_layout(render_layout),
                category_id=category_id,
                entity_id=entity_id,
                is_active=None if is_active is None else _coerce_bool(is_active),
                is_pinned=None if is_pinned is None else _coerce_bool(is_pinned),
                is_draft=None if is_draft is None else _coerce_bool(is_draft),
            )
            return _format_form_detail(item)

        if action == "delete":
            result = await form_service.delete_form(
                _require_id(form_id, "form_id"), purge=purge
            )
            return _format_mutation(action, result)

        # ---- section writes ---------------------------------------------
        if action == "create_section":
            if not name:
                return create_mcp_error(
                    "'name' e obrigatorio para criar uma secao",
                    "Forneca o titulo da secao",
                    "Exemplo: glpi_manage_forms(action='create_section', "
                    "form_id=1, name='Dados do solicitante')",
                )
            item = await form_service.create_section(
                _require_id(form_id, "form_id"),
                name=name,
                description=description,
                rank=rank,
            )
            return _format_simple_detail("Secao", item)

        if action == "update_section":
            item = await form_service.update_section(
                _require_id(section_id, "section_id"),
                name=name,
                description=description,
                rank=rank,
            )
            return _format_simple_detail("Secao", item)

        if action == "delete_section":
            result = await form_service.delete_section(
                _require_id(section_id, "section_id"), purge=purge
            )
            return _format_mutation(action, result)

        # ---- question writes --------------------------------------------
        if action == "create_question":
            if not name or not type:
                return create_mcp_error(
                    "'name' e 'type' sao obrigatorios para criar uma pergunta",
                    "Forneca o texto e o tipo (text, email, number, date, radio, "
                    "checkbox, dropdown, item, assignee, requester, observer, "
                    "urgency, request_type, file, user_device, long_answer)",
                    "Exemplo: glpi_manage_forms(action='create_question', "
                    "section_id=3, name='E-mail', type='email')",
                )
            resolve_question_type(type)  # valida antes de tocar o GLPI
            item = await form_service.create_question(
                section_id=_require_id(section_id, "section_id"),
                name=name,
                type=type,
                description=description,
                is_mandatory=is_mandatory,
                default_value=default_value,
                extra_data=extra_data,
                options=options,
                is_multiple_dropdown=is_multiple_dropdown,
                conditions=conditions,
                validation_conditions=validation_conditions,
                vertical_rank=vertical_rank,
                horizontal_rank=horizontal_rank,
            )
            return _format_simple_detail("Pergunta", item)

        if action == "update_question":
            item = await form_service.update_question(
                _require_id(question_id, "question_id"),
                name=name,
                description=description,
                type=type,
                is_mandatory=is_mandatory,
                default_value=default_value,
                extra_data=extra_data,
                options=options,
                is_multiple_dropdown=is_multiple_dropdown,
                conditions=conditions,
                validation_conditions=validation_conditions,
                vertical_rank=vertical_rank,
                horizontal_rank=horizontal_rank,
            )
            return _format_simple_detail("Pergunta", item)

        if action == "delete_question":
            result = await form_service.delete_question(
                _require_id(question_id, "question_id"), purge=purge
            )
            return _format_mutation(action, result)

        # ---- comment writes ---------------------------------------------
        if action == "create_comment":
            item = await form_service.create_comment(
                section_id=_require_id(section_id, "section_id"),
                name=name,
                description=description,
                conditions=conditions,
                vertical_rank=vertical_rank,
                horizontal_rank=horizontal_rank,
            )
            return _format_simple_detail("Comentario", item)

        if action == "update_comment":
            item = await form_service.update_comment(
                _require_id(comment_id, "comment_id"),
                name=name,
                description=description,
                conditions=conditions,
            )
            return _format_simple_detail("Comentario", item)

        if action == "delete_comment":
            result = await form_service.delete_comment(
                _require_id(comment_id, "comment_id"), purge=purge
            )
            return _format_mutation(action, result)

        # ---- category writes --------------------------------------------
        if action == "create_category":
            if not name:
                return create_mcp_error(
                    "'name' e obrigatorio para criar uma categoria",
                    "Forneca o nome da categoria do catalogo",
                    "Exemplo: glpi_manage_forms(action='create_category', name='TI')",
                )
            item = await form_service.create_category(
                name=name,
                parent_id=parent_id,
                description=description,
                entity_id=entity_id,
            )
            return _format_simple_detail("Categoria", item)

        if action == "update_category":
            item = await form_service.update_category(
                _require_id(category_id, "category_id"),
                name=name,
                description=description,
                parent_id=parent_id,
            )
            return _format_simple_detail("Categoria", item)

        if action == "delete_category":
            result = await form_service.delete_category(
                _require_id(category_id, "category_id"), purge=purge
            )
            return _format_mutation(action, result)

        # ---- destination writes (aba "Chamado") ---------------------------
        if action == "list_destinations":
            destinations = await form_service.list_destinations(
                _require_id(form_id, "form_id")
            )
            if not destinations:
                return "Nenhum destino encontrado no formulario."
            lines = [f"**{len(destinations)} destino(s)** do formulario {form_id}:"]
            for index, destination in enumerate(destinations, start=1):
                d_name = strip_html(str(destination.get("name") or "")).strip() or "(sem titulo)"
                lines.append(f"{index}. {esc(d_name)} (ID {destination.get('id')})")
            return "\n".join(lines)

        if action == "get_destination":
            item = await form_service.get_destination(
                _require_id(destination_id, "destination_id")
            )
            return _format_destination_detail(item)

        if action == "update_destination":
            item = await form_service.update_destination(
                _require_id(destination_id, "destination_id"),
                name=name,
                config=config,
                urgency_question_id=urgency_question_id,
                urgency_strategy=urgency_strategy,
            )
            return _format_destination_detail(item)

        return create_mcp_error(
            f"Acao '{action}' nao tratada",
            f"Esperado um de: {MANAGE_ACTIONS}",
            "Exemplo: glpi_manage_forms(action='get', form_id=1)",
        )

    except (NotFoundError, ValidationError) as exc:
        logger.error(f"manage_forms ({action}) error: {exc.message}")
        raise
    except GLPIError as exc:
        logger.error(f"manage_forms ({action}) GLPI error: {exc.message}")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(f"manage_forms ({action}) unexpected error: {exc}")
        raise GLPIError(500, f"Falha ao executar '{action}' em formularios: {exc}")
