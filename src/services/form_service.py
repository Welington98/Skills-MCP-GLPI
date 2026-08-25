"""
Form Service - CRUD dos formularios nativos do GLPI 11 (modulo Forms + Service Catalog).

Por que este modulo existe
--------------------------
Desde o GLPI 11 os formularios sao nativos (o plugin Formcreator chegou ao fim
de vida em 2.13.8) e alimentam o catalogo de servicos. Este servico expoe CRUD
para o formulario, suas secoes/perguntas/comentarios e as categorias do
catalogo, por cima da API legada V1 do GLPI.

Itemtypes
---------
As classes do modulo vivem em ``Glpi\\Form\\*`` e o GLPI as expoe na API como
itemtypes namespaced (FQCN). A API legada V1 aceita FQCN na URL desde que o
webserver decodifique o ``%5C`` do path — por isso todos os endpoints passam o
itemtype URL-encoded (``Glpi%5CForm%5CForm``). A resolucao (``getItemtype``)
valida via ``class_exists()`` + ``is_subclass_of(CommonDBTM)``, sem whitelist.

  * ``Glpi\\Form\\Form``      -- o formulario em si. Um POST cria, por padrao,
                            uma Section inicial, um FormDestination (ticket) e
                            um FormAccessControl (veja ``_init_*``).
  * ``Glpi\\Form\\Section``   -- secao; pai via ``forms_forms_id``.
  * ``Glpi\\Form\\Question``  -- pergunta; pai via ``forms_sections_id``. O
                            campo ``type`` e o FQCN de um QuestionType, p.ex.
                            ``Glpi\\Form\\QuestionType\\QuestionTypeShortText``.
  * ``Glpi\\Form\\Comment``   -- comentario informativo; pai via ``forms_sections_id``.
  * ``Glpi\\Form\\Category``  -- categoria do catalogo (CommonTreeDropdown).

``Glpi\\Form\\AnswersSet`` (as respostas enviadas) NAO e gerenciada via REST:
``canCreate/canUpdate/canDelete`` retornam ``false``. Este servico mexe na
DEFINICAO do formulario, nao nas respostas.

Os ids de campo de busca sao reconciliados em runtime contra
listSearchOptions, exatamente como nos outros servicos, porque os numeros
variam entre versoes, perfis e plugins.
"""

from __future__ import annotations

import asyncio
import json
import uuid as uuid_lib
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from src.models.exceptions import GLPIError, NotFoundError, ValidationError
from src.services.glpi_client import glpi_client
from src.services.search_options import search_options_cache
from src.utils.helpers import logger
from src.utils.search_criteria import normalize_order, resolve_sort_field

# ---------------------------------------------------------------------------
# Itemtypes nativos do modulo Forms (GLPI 11)
# ---------------------------------------------------------------------------

FORM_ITEMTYPE = "Glpi\\Form\\Form"
SECTION_ITEMTYPE = "Glpi\\Form\\Section"
QUESTION_ITEMTYPE = "Glpi\\Form\\Question"
COMMENT_ITEMTYPE = "Glpi\\Form\\Comment"
CATEGORY_ITEMTYPE = "Glpi\\Form\\Category"

_QUESTION_TYPE_NS = "Glpi\\Form\\QuestionType\\"

#: Nome amigavel (sem acento, aceita variantes) -> classe do QuestionType.
#: A classe vira o FQCN `Glpi\Form\QuestionType\<classe>`, que e o valor que o
#: GLPI espera no campo `type` da pergunta.
QUESTION_TYPE_CLASS = {
    "text": "QuestionTypeShortText",
    "short": "QuestionTypeShortText",
    "short_answer": "QuestionTypeShortText",
    "shorttext": "QuestionTypeShortText",
    "resposta_curta": "QuestionTypeShortText",
    "long": "QuestionTypeLongText",
    "long_answer": "QuestionTypeLongText",
    "longtext": "QuestionTypeLongText",
    "resposta_longa": "QuestionTypeLongText",
    "email": "QuestionTypeEmail",
    "number": "QuestionTypeNumber",
    "numero": "QuestionTypeNumber",
    "date": "QuestionTypeDateTime",
    "datetime": "QuestionTypeDateTime",
    "date_and_time": "QuestionTypeDateTime",
    "data": "QuestionTypeDateTime",
    "data_hora": "QuestionTypeDateTime",
    "radio": "QuestionTypeRadio",
    "checkbox": "QuestionTypeCheckbox",
    "check": "QuestionTypeCheckbox",
    "dropdown": "QuestionTypeDropdown",
    "select": "QuestionTypeDropdown",
    "lista": "QuestionTypeDropdown",
    "item": "QuestionTypeItem",
    "glpi_object": "QuestionTypeItem",
    "objeto_glpi": "QuestionTypeItem",
    "item_dropdown": "QuestionTypeItemDropdown",
    "assignee": "QuestionTypeAssignee",
    "requester": "QuestionTypeRequester",
    "observer": "QuestionTypeObserver",
    "atribuido": "QuestionTypeAssignee",
    "solicitante": "QuestionTypeRequester",
    "observador": "QuestionTypeObserver",
    "urgency": "QuestionTypeUrgency",
    "urgencia": "QuestionTypeUrgency",
    "request_type": "QuestionTypeRequestType",
    "tipo_solicitacao": "QuestionTypeRequestType",
    "file": "QuestionTypeFile",
    "document": "QuestionTypeFile",
    "arquivo": "QuestionTypeFile",
    "user_device": "QuestionTypeUserDevice",
    "users_devices": "QuestionTypeUserDevice",
    "dispositivo_usuario": "QuestionTypeUserDevice",
}

#: Tipos que aceitam uma lista de opcoes (radio/checkbox/dropdown).
_SELECTABLE_TYPES = {
    "QuestionTypeRadio",
    "QuestionTypeCheckbox",
    "QuestionTypeDropdown",
}

#: Valores aceitos pela coluna ``render_layout`` do Form (enum Glpi\Form\RenderLayout).
#: Normalizados para o valor que o GLPI grava na API.
RENDER_LAYOUTS = {
    "single_page": "single_page",
    "single-page": "single_page",
    "singlepage": "single_page",
    "pagina_unica": "single_page",
    "pagina_única": "single_page",
    "step_by_step": "step_by_step",
    "step-by-step": "step_by_step",
    "stepbystep": "step_by_step",
    "por_secao": "step_by_step",
    "por_seção": "step_by_step",
    "sections": "step_by_step",
}

RENDER_LAYOUT_LABELS = {
    "single_page": "pagina unica",
    "step_by_step": "secao por secao (padrao)",
}


def resolve_question_type(value: Any) -> str:
    """Map a friendly question type name to its FQCN, or validate an FQCN.

    Raises:
        ValidationError when the value does not name a known question type.
    """
    if value is None:
        raise ValidationError("O tipo da pergunta ('type') e obrigatorio", "type")

    text = str(value).strip()
    if not text:
        raise ValidationError("O tipo da pergunta ('type') e obrigatorio", "type")

    # FQCN completo: Glpi\Form\QuestionType\QuestionTypeShortText
    if text.startswith(_QUESTION_TYPE_NS) or "QuestionType" in text:
        fqcn = text if text.startswith(_QUESTION_TYPE_NS) else _QUESTION_TYPE_NS + text
        cls = fqcn.rsplit("\\", 1)[-1]
        if cls in _SELECTABLE_TYPES or cls in QUESTION_TYPE_CLASS.values():
            return fqcn
        raise ValidationError(
            f"Tipo de pergunta '{text}' desconhecido. Use um nome amigavel "
            "(text, email, number, date, radio, checkbox, dropdown, item, "
            "assignee, requester, observer, urgency, request_type, file, "
            "user_device, long_answer) ou o FQCN do QuestionType.",
            "type",
        )

    cls = QUESTION_TYPE_CLASS.get(text.lower().replace(" ", "_"))
    if cls is None:
        raise ValidationError(
            f"Tipo de pergunta '{value}' desconhecido. Disponiveis: "
            f"{', '.join(sorted(set(QUESTION_TYPE_CLASS)))}",
            "type",
        )
    return _QUESTION_TYPE_NS + cls


def resolve_render_layout(value: Any) -> Optional[str]:
    """Normalise a friendly form layout name to the value GLPI stores.

    Returns ``None`` when the caller did not set the field (the GLPI default,
    ``step_by_step``, then applies on create). Unknown values are refused so a
    typo does not silently reach the API and get stored in the table.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(
            "render_layout deve ser uma string: single_page ou step_by_step",
            "render_layout",
        )
    text = str(value).strip().lower().replace(" ", "_")
    layout = RENDER_LAYOUTS.get(text)
    if layout is None:
        raise ValidationError(
            f"render_layout '{value}' desconhecido. Valores aceitos: "
            "single_page (pagina unica) ou step_by_step (secao por secao).",
            "render_layout",
        )
    return layout


# ---------------------------------------------------------------------------
# Field maps (reconciliados contra listSearchOptions em runtime)
# ---------------------------------------------------------------------------

#: Form::rawSearchOptions() -- GLPI 11.
FORM_FIELD: Dict[str, int] = {
    "name": 1,
    "id": 2,
    "is_active": 3,
    "date_mod": 4,
    "date_creation": 5,
    "category": 6,   # Category.completename (join)
    "entity": 80,    # Entity.completename (join)
}

#: Somente colunas da propria tabela recebem hint: sao as que o catalogo
#: consegue re-derivar sem ambiguidade. category (6) e entity (80) chegam por
#: join e sao apenas verificadas quanto a existencia.
_FORM_UID_HINTS = {
    "name": "Glpi\\Form\\Form.name",
    "id": "Glpi\\Form\\Form.id",
    "is_active": "Glpi\\Form\\Form.is_active",
    "date_mod": "Glpi\\Form\\Form.date_mod",
    "date_creation": "Glpi\\Form\\Form.date_creation",
}

#: Category (CommonTreeDropdown) -- ids da propria tabela reconciliados.
CATEGORY_FIELD: Dict[str, int] = {
    "name": 1,
    "id": 2,
    "description": 3,
    "entity": 80,
    "date_mod": 19,
    "date_creation": 121,
}

_CATEGORY_UID_HINTS = {
    "name": "Glpi\\Form\\Category.name",
    "id": "Glpi\\Form\\Category.id",
    "description": "Glpi\\Form\\Category.description",
    "entity": "Glpi\\Form\\Category.entities_id",
    "date_mod": "Glpi\\Form\\Category.date_mod",
    "date_creation": "Glpi\\Form\\Category.date_creation",
}

#: Encoded itemtypes usados nos endpoints.
FORM_ENC = quote(FORM_ITEMTYPE, safe="")
SECTION_ENC = quote(SECTION_ITEMTYPE, safe="")
QUESTION_ENC = quote(QUESTION_ITEMTYPE, safe="")
COMMENT_ENC = quote(COMMENT_ITEMTYPE, safe="")
CATEGORY_ENC = quote(CATEGORY_ITEMTYPE, safe="")

_form_field_sync_done = False
_form_field_sync_lock = asyncio.Lock()

_category_field_sync_done = False
_category_field_sync_lock = asyncio.Lock()


async def ensure_form_field_map_synced() -> None:
    """Correct FORM_FIELD against the instance's catalogue, once."""
    global _form_field_sync_done
    if _form_field_sync_done:
        return
    async with _form_field_sync_lock:
        if _form_field_sync_done:
            return
        try:
            await search_options_cache.reconcile(FORM_ENC, FORM_FIELD, _FORM_UID_HINTS)
        except Exception as exc:  # noqa: BLE001 -- never block a search
            logger.warning(f"Form field reconciliation skipped: {exc}")
        finally:
            _form_field_sync_done = True


async def ensure_category_field_map_synced() -> None:
    """Correct CATEGORY_FIELD against the instance's catalogue, once."""
    global _category_field_sync_done
    if _category_field_sync_done:
        return
    async with _category_field_sync_lock:
        if _category_field_sync_done:
            return
        try:
            await search_options_cache.reconcile(CATEGORY_ENC, CATEGORY_FIELD, _CATEGORY_UID_HINTS)
        except Exception as exc:  # noqa: BLE001 -- never block a search
            logger.warning(f"Category field reconciliation skipped: {exc}")
        finally:
            _category_field_sync_done = True


def reset_field_sync() -> None:
    """Forget the reconciliation (tests and cache invalidation)."""
    global _form_field_sync_done, _category_field_sync_done
    _form_field_sync_done = False
    _category_field_sync_done = False


# ---------------------------------------------------------------------------
# Erros e helpers
# ---------------------------------------------------------------------------


def _itemtype_not_available(exc: Exception) -> bool:
    """Detect the 400 GLPI returns when the itemtype cannot be resolved.

    A API legada nao decodifica o path: quem decodifica o `%5C` e o webserver.
    Se o proxy deixar o segmento como `Glpi%5CForm%5CForm`, o GLPI responde
    ERROR_RESOURCE_NOT_FOUND_NOR_COMMONDBTM. Isso nao e "formulario nao existe";
    e o ambiente que nao encaminha o encoding.
    """
    return (
        isinstance(exc, GLPIError)
        and getattr(exc, "code", None) == 400
        and "COMMONDBTM" in str(getattr(exc, "message", "") or "")
    )


def _raise_form_endpoint_error(exc: Exception) -> None:
    """Translate the namespaced-itemtype rejection into a clear message."""
    if _itemtype_not_available(exc):
        raise GLPIError(
            400,
            "Este GLPI nao consegue resolver o itemtype do modulo Forms "
            "(`Glpi\\Form\\Form`). O webserver precisa decodificar o `%5C` "
            "(percent-encoding) no caminho da API — verifique o proxy/nginx "
            "entre este MCP e o GLPI.",
        ) from exc


def _require_id(value: Any, label: str) -> int:
    """Validate a positive integer id, mirroring the other services."""
    if isinstance(value, bool):
        raise ValidationError(f"{label} deve ser um inteiro positivo", label)
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} deve ser um inteiro positivo", label)
    if ivalue <= 0:
        raise ValidationError(f"{label} deve ser um inteiro positivo", label)
    return ivalue


def _clean_rows(payload: Any) -> List[Dict[str, Any]]:
    """Normalise a Search API response into a list of row dicts."""
    if isinstance(payload, dict) and "data" in payload:
        rows = payload["data"]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _total_count(payload: Any) -> int:
    if isinstance(payload, dict):
        try:
            return int(payload.get("totalcount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


class FormService:
    """CRUD dos formularios nativos do GLPI e do catalogo de servicos."""

    def __init__(self) -> None:
        logger.info("FormService initialized")

    # ======================================================================
    # FORMULARIOS
    # ======================================================================

    async def search_forms(
        self,
        query: Optional[str] = None,
        entity_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        category_id: Optional[int] = None,
        limit: int = 10,
        offset: int = 0,
        sort_by: Optional[str] = None,
        order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List / text-search forms, returning a paginated payload."""
        await ensure_form_field_map_synced()

        criteria: List[Dict[str, Any]] = []
        if query:
            criteria.append({
                "field": FORM_FIELD["name"],
                "searchtype": "contains",
                "value": query,
            })
        if is_active is not None:
            criteria.append({
                "field": FORM_FIELD["is_active"],
                "searchtype": "equals",
                "value": int(bool(is_active)),
            })
        if category_id is not None:
            criteria.append({
                "field": FORM_FIELD["category"],
                "searchtype": "equals",
                "value": category_id,
            })
        if entity_id is not None:
            criteria.append({
                "field": FORM_FIELD["entity"],
                "searchtype": "under",
                "value": entity_id,
            })

        sort_field = resolve_sort_field(
            sort_by, FORM_FIELD, FORM_FIELD["date_mod"], context="search_forms"
        )
        sort_order = normalize_order(order, default="DESC")

        try:
            result = await glpi_client.search(
                item_type=FORM_ENC,
                criteria=criteria or None,
                forcedisplay=[
                    FORM_FIELD["id"],
                    FORM_FIELD["name"],
                    FORM_FIELD["category"],
                    FORM_FIELD["is_active"],
                    FORM_FIELD["date_mod"],
                ],
                range_limit=limit,
                range_offset=offset,
                is_recursive=entity_id is not None,
                sort=sort_field,
                order=sort_order,
                expand_dropdowns=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"search_forms failed: {exc}")
            _raise_form_endpoint_error(exc)
            raise

        items = []
        for row in _clean_rows(result):
            items.append({
                "id": row.get(str(FORM_FIELD["id"])) or row.get("id"),
                "name": row.get(str(FORM_FIELD["name"])) or "",
                "category": row.get(str(FORM_FIELD["category"])) or "",
                "is_active": row.get(str(FORM_FIELD["is_active"])),
                "date_mod": row.get(str(FORM_FIELD["date_mod"])),
            })

        return {
            "scope": "forms",
            "items": items,
            "total": _total_count(result),
            "limit": limit,
            "offset": offset,
        }

    async def get_form(self, form_id: int) -> Dict[str, Any]:
        """Fetch one form with its sections, questions and comments embedded."""
        form_id = _require_id(form_id, "form_id")
        try:
            item = await glpi_client.get(
                f"/apirest.php/{FORM_ENC}/{form_id}",
                params={"expand_dropdowns": 1},
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            _raise_form_endpoint_error(exc)
            raise

        if not isinstance(item, dict) or "id" not in item:
            raise NotFoundError(FORM_ITEMTYPE, form_id)

        item["sections"] = await self._get_sections(form_id)
        return item

    async def _get_sections(self, form_id: int) -> List[Dict[str, Any]]:
        """List a form's sections, with their questions/comments (best-effort)."""
        try:
            sections = await glpi_client.get(
                f"/apirest.php/{FORM_ENC}/{form_id}/{SECTION_ENC}",
                params={"range": "0-49"},
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"get_form({form_id}): sections indisponiveis: {exc}")
            return []

        out = []
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict) or "id" not in section:
                continue
            section_id = int(section["id"])
            section["questions"] = await self._get_questions(section_id)
            section["comments"] = await self._get_comments(section_id)
            out.append(section)
        return out

    async def _get_questions(self, section_id: int) -> List[Dict[str, Any]]:
        try:
            questions = await glpi_client.get(
                f"/apirest.php/{SECTION_ENC}/{section_id}/{QUESTION_ENC}",
                params={"range": "0-99"},
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"section {section_id}: questions indisponiveis: {exc}")
            return []
        return [q for q in (questions if isinstance(questions, list) else []) if isinstance(q, dict)]

    async def _get_comments(self, section_id: int) -> List[Dict[str, Any]]:
        try:
            comments = await glpi_client.get(
                f"/apirest.php/{SECTION_ENC}/{section_id}/{COMMENT_ENC}",
                params={"range": "0-49"},
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"section {section_id}: comments indisponiveis: {exc}")
            return []
        return [c for c in (comments if isinstance(comments, list) else []) if isinstance(c, dict)]

    async def create_form(
        self,
        name: str,
        description: Optional[str] = None,
        header: Optional[str] = None,
        render_layout: Optional[str] = None,
        category_id: Optional[int] = None,
        entity_id: Optional[int] = None,
        is_active: bool = True,
        is_pinned: bool = False,
        is_draft: bool = False,
        init_sections: bool = True,
        init_destinations: bool = True,
        init_access_policies: bool = True,
    ) -> Dict[str, Any]:
        """Create a form.

        A API do GLPI cria por padrao uma Section inicial, um FormDestination
        (ticket) e um FormAccessControl. Os parametros `init_*` desativam cada
        um desses defaults.
        """
        if not name or len(str(name).strip()) < 2:
            raise ValidationError("O nome do formulario deve ter pelo menos 2 caracteres", "name")

        render_layout = resolve_render_layout(render_layout)

        payload: Dict[str, Any] = {"name": str(name).strip()}
        if description is not None:
            payload["description"] = description
        if header is not None:
            payload["header"] = header
        if render_layout is not None:
            payload["render_layout"] = render_layout
        if category_id is not None:
            payload["forms_categories_id"] = _require_id(category_id, "category_id")
        if entity_id is not None:
            payload["entities_id"] = entity_id
        if is_active is not None:
            payload["is_active"] = int(bool(is_active))
        if is_pinned:
            payload["is_pinned"] = 1
        if is_draft:
            payload["is_draft"] = 1
        if not init_sections:
            payload["_init_sections"] = False
        if not init_destinations:
            payload["_init_destinations"] = False
        if not init_access_policies:
            payload["_init_access_policies"] = False

        try:
            result = await glpi_client.post(f"/apirest.php/{FORM_ENC}", payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"create_form failed: {exc}")
            _raise_form_endpoint_error(exc)
            raise

        if not isinstance(result, dict) or "id" not in result:
            raise GLPIError(500, "Formulario criado sem ID retornado pelo GLPI")
        return await self.get_form(int(result["id"]))

    async def update_form(self, form_id: int, **fields: Any) -> Dict[str, Any]:
        """Update form metadata fields."""
        form_id = _require_id(form_id, "form_id")
        payload = self._form_payload(**fields)
        if not payload:
            raise ValidationError(
                "Nenhum campo atualizavel fornecido para o formulario", "payload"
            )
        try:
            await glpi_client.put(f"/apirest.php/{FORM_ENC}/{form_id}", payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"update_form({form_id}) failed: {exc}")
            _raise_form_endpoint_error(exc)
            raise
        return await self.get_form(form_id)

    @staticmethod
    def _form_payload(**fields: Any) -> Dict[str, Any]:
        """Map update/create fields for a Form to its GLPI columns."""
        payload: Dict[str, Any] = {}
        mapping = {
            "name": "name",
            "description": "description",
            "header": "header",
            "render_layout": "render_layout",
            "category_id": "forms_categories_id",
            "entity_id": "entities_id",
        }
        for key, column in mapping.items():
            if fields.get(key) is not None:
                payload[column] = fields[key]
        for flag in ("is_active", "is_pinned", "is_draft"):
            if fields.get(flag) is not None:
                payload[flag] = int(bool(fields[flag]))
        if "render_layout" in payload:
            payload["render_layout"] = resolve_render_layout(payload["render_layout"])
        return payload

    async def delete_form(self, form_id: int, purge: bool = True) -> Dict[str, Any]:
        """Delete a form (purge cascades to sections/questions/comments)."""
        form_id = _require_id(form_id, "form_id")
        endpoint = f"/apirest.php/{FORM_ENC}/{form_id}"
        if purge:
            endpoint += "?force_purge=true"
        try:
            await glpi_client.delete(endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"delete_form({form_id}) failed: {exc}")
            _raise_form_endpoint_error(exc)
            raise
        return {"id": form_id, "purged": purge}

    # ======================================================================
    # SECOES
    # ======================================================================

    async def list_sections(self, form_id: int) -> List[Dict[str, Any]]:
        form_id = _require_id(form_id, "form_id")
        return await self._get_sections(form_id)

    async def get_section(self, section_id: int) -> Dict[str, Any]:
        section_id = _require_id(section_id, "section_id")
        item = await glpi_client.get(
            f"/apirest.php/{SECTION_ENC}/{section_id}", use_cache=False
        )
        if not isinstance(item, dict) or "id" not in item:
            raise NotFoundError(SECTION_ITEMTYPE, section_id)
        item["questions"] = await self._get_questions(section_id)
        item["comments"] = await self._get_comments(section_id)
        return item

    async def create_section(
        self,
        form_id: int,
        name: str,
        description: Optional[str] = None,
        rank: Optional[int] = None,
    ) -> Dict[str, Any]:
        form_id = _require_id(form_id, "form_id")
        if not name or len(str(name).strip()) < 2:
            raise ValidationError("O nome da secao deve ter pelo menos 2 caracteres", "name")
        payload: Dict[str, Any] = {
            "forms_forms_id": form_id,
            "name": str(name).strip(),
        }
        if description is not None:
            payload["description"] = description
        if rank is not None:
            payload["rank"] = int(rank)
        result = await glpi_client.post(f"/apirest.php/{SECTION_ENC}", payload)
        if not isinstance(result, dict) or "id" not in result:
            raise GLPIError(500, "Secao criada sem ID retornado pelo GLPI")
        return await self.get_section(int(result["id"]))

    async def update_section(self, section_id: int, **fields: Any) -> Dict[str, Any]:
        section_id = _require_id(section_id, "section_id")
        payload: Dict[str, Any] = {}
        if fields.get("name") is not None:
            payload["name"] = fields["name"]
        if fields.get("description") is not None:
            payload["description"] = fields["description"]
        if fields.get("rank") is not None:
            payload["rank"] = int(fields["rank"])
        if not payload:
            raise ValidationError("Nenhum campo atualizavel fornecido para a secao", "payload")
        await glpi_client.put(f"/apirest.php/{SECTION_ENC}/{section_id}", payload)
        return await self.get_section(section_id)

    async def delete_section(self, section_id: int, purge: bool = True) -> Dict[str, Any]:
        section_id = _require_id(section_id, "section_id")
        endpoint = f"/apirest.php/{SECTION_ENC}/{section_id}"
        if purge:
            endpoint += "?force_purge=true"
        await glpi_client.delete(endpoint)
        return {"id": section_id, "purged": purge}

    # ======================================================================
    # PERGUNTAS
    # ======================================================================

    async def create_question(
        self,
        section_id: int,
        name: str,
        type: str,
        description: Optional[str] = None,
        default_value: Any = None,
        extra_data: Optional[Dict[str, Any]] = None,
        options: Optional[List[str]] = None,
        is_multiple_dropdown: Optional[bool] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        validation_conditions: Optional[List[Dict[str, Any]]] = None,
        vertical_rank: Optional[int] = None,
        horizontal_rank: Optional[int] = None,
    ) -> Dict[str, Any]:
        section_id = _require_id(section_id, "section_id")
        if not name or len(str(name).strip()) < 2:
            raise ValidationError("O nome da pergunta deve ter pelo menos 2 caracteres", "name")
        payload = self._question_payload(
            section_id=section_id,
            name=name,
            type=type,
            description=description,
            default_value=default_value,
            extra_data=extra_data,
            options=options,
            is_multiple_dropdown=is_multiple_dropdown,
            conditions=conditions,
            validation_conditions=validation_conditions,
            vertical_rank=vertical_rank,
            horizontal_rank=horizontal_rank,
        )
        result = await glpi_client.post(f"/apirest.php/{QUESTION_ENC}", payload)
        if not isinstance(result, dict) or "id" not in result:
            raise GLPIError(500, "Pergunta criada sem ID retornado pelo GLPI")
        return await self.get_question(int(result["id"]))

    async def update_question(self, question_id: int, **fields: Any) -> Dict[str, Any]:
        question_id = _require_id(question_id, "question_id")
        payload = self._question_payload(**fields)
        if not payload:
            raise ValidationError("Nenhum campo atualizavel fornecido para a pergunta", "payload")
        await glpi_client.put(f"/apirest.php/{QUESTION_ENC}/{question_id}", payload)
        return await self.get_question(question_id)

    async def get_question(self, question_id: int) -> Dict[str, Any]:
        question_id = _require_id(question_id, "question_id")
        item = await glpi_client.get(
            f"/apirest.php/{QUESTION_ENC}/{question_id}", use_cache=False
        )
        if not isinstance(item, dict) or "id" not in item:
            raise NotFoundError(QUESTION_ITEMTYPE, question_id)
        return item

    async def delete_question(self, question_id: int, purge: bool = True) -> Dict[str, Any]:
        question_id = _require_id(question_id, "question_id")
        endpoint = f"/apirest.php/{QUESTION_ENC}/{question_id}"
        if purge:
            endpoint += "?force_purge=true"
        await glpi_client.delete(endpoint)
        return {"id": question_id, "purged": purge}

    @staticmethod
    def _question_payload(**fields: Any) -> Dict[str, Any]:
        """Build the Question payload (type -> FQCN, JSON columns encoded)."""
        payload: Dict[str, Any] = {}

        name = fields.get("name")
        if name is not None:
            payload["name"] = name
        if fields.get("description") is not None:
            payload["description"] = fields["description"]

        if fields.get("section_id") is not None:
            payload["forms_sections_id"] = _require_id(fields["section_id"], "section_id")
        if fields.get("forms_sections_id") is not None:
            payload["forms_sections_id"] = fields["forms_sections_id"]

        type_value = fields.get("type")
        if type_value is not None:
            payload["type"] = resolve_question_type(type_value)

        if fields.get("vertical_rank") is not None:
            payload["vertical_rank"] = int(fields["vertical_rank"])
        if fields.get("horizontal_rank") is not None:
            payload["horizontal_rank"] = int(fields["horizontal_rank"])

        # Opcoes de radio/checkbox/dropdown viram extra_data.options com uuid.
        if fields.get("options") is not None or fields.get("extra_data") is not None:
            extra: Dict[str, Any] = dict(fields.get("extra_data") or {})
            options = fields.get("options")
            if options is not None:
                extra["options"] = {
                    str(uuid_lib.uuid4()): str(label) for label in options
                }
            if fields.get("is_multiple_dropdown") is not None:
                extra["is_multiple_dropdown"] = int(bool(fields["is_multiple_dropdown"]))
            if extra:
                payload["extra_data"] = extra

        if fields.get("default_value") is not None:
            payload["default_value"] = fields["default_value"]

        if fields.get("conditions") is not None:
            payload["conditions"] = json.dumps(fields["conditions"], ensure_ascii=False)
        if fields.get("validation_conditions") is not None:
            payload["validation_conditions"] = json.dumps(
                fields["validation_conditions"], ensure_ascii=False
            )
        return payload

    # ======================================================================
    # COMENTARIOS
    # ======================================================================

    async def create_comment(
        self,
        section_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        vertical_rank: Optional[int] = None,
        horizontal_rank: Optional[int] = None,
    ) -> Dict[str, Any]:
        section_id = _require_id(section_id, "section_id")
        payload: Dict[str, Any] = {"forms_sections_id": section_id}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if conditions is not None:
            payload["conditions"] = json.dumps(conditions, ensure_ascii=False)
        if vertical_rank is not None:
            payload["vertical_rank"] = int(vertical_rank)
        if horizontal_rank is not None:
            payload["horizontal_rank"] = int(horizontal_rank)
        result = await glpi_client.post(f"/apirest.php/{COMMENT_ENC}", payload)
        if not isinstance(result, dict) or "id" not in result:
            raise GLPIError(500, "Comentario criado sem ID retornado pelo GLPI")
        return await self.get_comment(int(result["id"]))

    async def update_comment(self, comment_id: int, **fields: Any) -> Dict[str, Any]:
        comment_id = _require_id(comment_id, "comment_id")
        payload: Dict[str, Any] = {}
        if fields.get("name") is not None:
            payload["name"] = fields["name"]
        if fields.get("description") is not None:
            payload["description"] = fields["description"]
        if fields.get("conditions") is not None:
            payload["conditions"] = json.dumps(fields["conditions"], ensure_ascii=False)
        if not payload:
            raise ValidationError("Nenhum campo atualizavel fornecido para o comentario", "payload")
        await glpi_client.put(f"/apirest.php/{COMMENT_ENC}/{comment_id}", payload)
        return await self.get_comment(comment_id)

    async def get_comment(self, comment_id: int) -> Dict[str, Any]:
        comment_id = _require_id(comment_id, "comment_id")
        item = await glpi_client.get(
            f"/apirest.php/{COMMENT_ENC}/{comment_id}", use_cache=False
        )
        if not isinstance(item, dict) or "id" not in item:
            raise NotFoundError(COMMENT_ITEMTYPE, comment_id)
        return item

    async def delete_comment(self, comment_id: int, purge: bool = True) -> Dict[str, Any]:
        comment_id = _require_id(comment_id, "comment_id")
        endpoint = f"/apirest.php/{COMMENT_ENC}/{comment_id}"
        if purge:
            endpoint += "?force_purge=true"
        await glpi_client.delete(endpoint)
        return {"id": comment_id, "purged": purge}

    # ======================================================================
    # CATEGORIAS DO CATALOGO
    # ======================================================================

    async def list_categories(
        self,
        query: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
        sort_by: Optional[str] = None,
        order: Optional[str] = None,
    ) -> Dict[str, Any]:
        await ensure_category_field_map_synced()

        criteria: List[Dict[str, Any]] = []
        if query:
            criteria.append({
                "field": CATEGORY_FIELD["name"],
                "searchtype": "contains",
                "value": query,
            })

        sort_field = resolve_sort_field(
            sort_by, CATEGORY_FIELD, CATEGORY_FIELD["name"], context="search_forms[category]"
        )
        sort_order = normalize_order(order, default="ASC")

        result = await glpi_client.search(
            item_type=CATEGORY_ENC,
            criteria=criteria or None,
            forcedisplay=[
                CATEGORY_FIELD["id"],
                CATEGORY_FIELD["name"],
                CATEGORY_FIELD["description"],
                CATEGORY_FIELD["entity"],
            ],
            range_limit=limit,
            range_offset=offset,
            sort=sort_field,
            order=sort_order,
            expand_dropdowns=True,
        )

        items = []
        for row in _clean_rows(result):
            items.append({
                "id": row.get(str(CATEGORY_FIELD["id"])) or row.get("id"),
                "name": row.get(str(CATEGORY_FIELD["name"])) or "",
                "description": row.get(str(CATEGORY_FIELD["description"])) or "",
                "entity": row.get(str(CATEGORY_FIELD["entity"])) or "",
            })

        return {
            "scope": "categories",
            "items": items,
            "total": _total_count(result),
            "limit": limit,
            "offset": offset,
        }

    async def get_category(self, category_id: int) -> Dict[str, Any]:
        category_id = _require_id(category_id, "category_id")
        item = await glpi_client.get(
            f"/apirest.php/{CATEGORY_ENC}/{category_id}", use_cache=False
        )
        if not isinstance(item, dict) or "id" not in item:
            raise NotFoundError(CATEGORY_ITEMTYPE, category_id)
        return item

    async def create_category(
        self,
        name: str,
        parent_id: Optional[int] = None,
        description: Optional[str] = None,
        entity_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not name or len(str(name).strip()) < 2:
            raise ValidationError("O nome da categoria deve ter pelo menos 2 caracteres", "name")
        payload: Dict[str, Any] = {"name": str(name).strip()}
        if parent_id is not None:
            payload["forms_categories_id"] = _require_id(parent_id, "parent_id")
        if description is not None:
            payload["description"] = description
        if entity_id is not None:
            payload["entities_id"] = entity_id
        result = await glpi_client.post(f"/apirest.php/{CATEGORY_ENC}", payload)
        if not isinstance(result, dict) or "id" not in result:
            raise GLPIError(500, "Categoria criada sem ID retornado pelo GLPI")
        return await self.get_category(int(result["id"]))

    async def update_category(self, category_id: int, **fields: Any) -> Dict[str, Any]:
        category_id = _require_id(category_id, "category_id")
        payload: Dict[str, Any] = {}
        if fields.get("name") is not None:
            payload["name"] = fields["name"]
        if fields.get("description") is not None:
            payload["description"] = fields["description"]
        if fields.get("parent_id") is not None:
            payload["forms_categories_id"] = _require_id(fields["parent_id"], "parent_id")
        if not payload:
            raise ValidationError("Nenhum campo atualizavel fornecido para a categoria", "payload")
        await glpi_client.put(f"/apirest.php/{CATEGORY_ENC}/{category_id}", payload)
        return await self.get_category(category_id)

    async def delete_category(self, category_id: int, purge: bool = True) -> Dict[str, Any]:
        category_id = _require_id(category_id, "category_id")
        endpoint = f"/apirest.php/{CATEGORY_ENC}/{category_id}"
        if purge:
            endpoint += "?force_purge=true"
        await glpi_client.delete(endpoint)
        return {"id": category_id, "purged": purge}


# Instancia global do servico de formularios
form_service = FormService()
