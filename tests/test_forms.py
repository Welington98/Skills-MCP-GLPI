"""
Tests for the native GLPI 11 Forms module (service catalog) tools.

What this suite is defending
---------------------------
The Forms module reaches the GLPI REST API through *namespaced* itemtypes
(``Glpi\\Form\\Form``). The legacy V1 API only resolves them when the web
server decodes the ``%5C`` in the URL path, so every endpoint must be built
with the percent-encoded itemtype. The most dangerous failure mode here is
silent: a plain ``/apirest.php/Glpi/Form/Form`` path or an unencoded backslash
would 400 with ERROR_RESOURCE_NOT_FOUND_NOR_COMMONDBTM and read like "form not
found". These tests pin the exact encoded endpoints and the clear error when
the environment cannot resolve the itemtype.

Other invariants under test:
  * friendly question types resolve to the QuestionType FQCN the GLPI stores;
  * radio/checkbox/dropdown options become ``extra_data.options`` with uuids;
  * create of a form passes the ``_init_*`` auto-bootstrap flags;
  * a delete cannot reach the service without the shared safety guard;
  * the write policy gates every form write, and the delete gates default OFF.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.models.exceptions import GLPIError, ValidationError
from src.security.write_policy import (
    WriteOperation,
    resolve_operation,
)
from src.services.form_service import (
    FORM_ENC,
    QUESTION_ENC,
    resolve_question_type,
    resolve_render_layout,
    form_service,
    reset_field_sync,
)
from src.tools.consolidated_forms import _format_mutation, manage_forms, search_forms
from src.utils.safety_guard import safety_guard


@pytest.fixture(autouse=True)
def hermetic_field_maps():
    """Keep every test off the network and off each other's field maps."""
    reset_field_sync()
    with patch(
        "src.services.form_service.search_options_cache.reconcile",
        new=AsyncMock(return_value={"corrected": {}, "missing": [], "checked": False}),
    ):
        yield
    reset_field_sync()


def _get_mock(payload=None):
    return AsyncMock(return_value=payload if payload is not None else {})


# ---------------------------------------------------------------------------
# Question type resolution
# ---------------------------------------------------------------------------


class TestResolveQuestionType:
    def test_friendly_names_map_to_fqcn(self) -> None:
        assert resolve_question_type("text") == (
            "Glpi\\Form\\QuestionType\\QuestionTypeShortText"
        )
        assert resolve_question_type("radio") == (
            "Glpi\\Form\\QuestionType\\QuestionTypeRadio"
        )
        assert resolve_question_type("Solicitante") == (
            "Glpi\\Form\\QuestionType\\QuestionTypeRequester"
        )
        assert resolve_question_type("date_and_time") == (
            "Glpi\\Form\\QuestionType\\QuestionTypeDateTime"
        )

    def test_fqcn_passes_through(self) -> None:
        fqcn = "Glpi\\Form\\QuestionType\\QuestionTypeShortText"
        assert resolve_question_type(fqcn) == fqcn

    def test_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            resolve_question_type("spaceship")

    def test_missing_type_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            resolve_question_type(None)


class TestResolveRenderLayout:
    def test_single_page_is_kept(self) -> None:
        assert resolve_render_layout("single_page") == "single_page"

    def test_friendly_aliases_normalise(self) -> None:
        assert resolve_render_layout("pagina unica") == "single_page"
        assert resolve_render_layout("SINGLE-PAGE") == "single_page"
        assert resolve_render_layout("step by step") == "step_by_step"

    def test_unset_returns_none(self) -> None:
        assert resolve_render_layout(None) is None

    def test_unknown_layout_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            resolve_render_layout("carrossel")

    def test_boolean_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            resolve_render_layout(True)


# ---------------------------------------------------------------------------
# Service layer — endpoints and payloads
# ---------------------------------------------------------------------------


class TestFormServiceEndpoints:
    async def test_search_forms_uses_encoded_itemtype(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.search",
            new=AsyncMock(return_value={"data": [], "totalcount": 0}),
        ) as search:
            await form_service.search_forms(limit=10, offset=0)

        search.assert_awaited_once()
        kwargs = search.await_args.kwargs
        assert kwargs["item_type"] == FORM_ENC
        assert kwargs["forcedisplay"] == [2, 1, 6, 3, 4]
        assert kwargs["range_limit"] == 10

    async def test_search_forms_builds_criteria(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.search",
            new=AsyncMock(return_value={"data": [], "totalcount": 0}),
        ) as search:
            await form_service.search_forms(
                query="vpn", is_active=True, category_id=7, entity_id=3, limit=5
            )

        criteria = search.await_args.kwargs["criteria"]
        assert {"field": 1, "searchtype": "contains", "value": "vpn"} in criteria
        assert {"field": 3, "searchtype": "equals", "value": 1} in criteria
        assert {"field": 6, "searchtype": "equals", "value": 7} in criteria
        assert {"field": 80, "searchtype": "under", "value": 3} in criteria

    async def test_search_forms_normalises_rows(self) -> None:
        payload = {
            "data": [
                {"1": "Acesso VPN", "2": "12", "6": "TI", "3": "1", "4": "2026-08-01 10:00:00"}
            ],
            "totalcount": 1,
        }
        with patch(
            "src.services.form_service.glpi_client.search",
            new=AsyncMock(return_value=payload),
        ):
            result = await form_service.search_forms()
        assert result["items"][0]["name"] == "Acesso VPN"
        assert result["items"][0]["id"] == "12"
        assert result["items"][0]["category"] == "TI"
        assert result["total"] == 1

    async def test_create_form_posts_encoded_endpoint_with_bootstrap_flags(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.post",
            new=AsyncMock(return_value={"id": 42}),
        ) as post, patch(
            "src.services.form_service.glpi_client.get",
            new=AsyncMock(return_value={"id": 42, "name": "Acesso VPN"}),
        ) as get:
            result = await form_service.create_form(
                name="Acesso VPN",
                category_id=2,
                is_pinned=True,
                init_destinations=False,
            )

        post.assert_awaited_once_with(
            f"/apirest.php/{FORM_ENC}",
            {
                "name": "Acesso VPN",
                "forms_categories_id": 2,
                "is_active": 1,
                "is_pinned": 1,
                "_init_destinations": False,
            },
        )
        get.assert_awaited()
        assert result["id"] == 42

    async def test_create_form_posts_render_layout_when_single_page(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.post",
            new=AsyncMock(return_value={"id": 42}),
        ) as post, patch(
            "src.services.form_service.glpi_client.get",
            new=AsyncMock(return_value={"id": 42, "name": "Acesso VPN"}),
        ):
            await form_service.create_form(
                name="Acesso VPN", render_layout="single_page"
            )

        payload = post.await_args.args[1]
        assert payload["render_layout"] == "single_page"

    async def test_update_form_posts_render_layout(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.put",
            new=AsyncMock(return_value={}),
        ) as put, patch(
            "src.services.form_service.glpi_client.get",
            new=AsyncMock(return_value={"id": 24, "name": "Form"}),
        ):
            await form_service.update_form(24, render_layout="single_page")

        put.assert_awaited_once()
        assert put.await_args.args[1] == {"render_layout": "single_page"}

    async def test_update_form_refuses_unknown_render_layout(self) -> None:
        with pytest.raises(ValidationError):
            await form_service.update_form(24, render_layout="carrossel")

    async def test_create_question_maps_type_and_options(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.post",
            new=AsyncMock(return_value={"id": 9}),
        ) as post, patch(
            "src.services.form_service.glpi_client.get",
            new=AsyncMock(return_value={"id": 9, "name": "Preferencia"}),
        ):
            await form_service.create_question(
                section_id=3,
                name="Preferencia",
                type="radio",
                options=["Sim", "Nao"],
            )

        payload = post.await_args.args[1]
        assert payload["forms_sections_id"] == 3
        assert payload["type"] == "Glpi\\Form\\QuestionType\\QuestionTypeRadio"
        assert set(payload["extra_data"]["options"].values()) == {"Sim", "Nao"}
        # is_multiple_dropdown so faz sentido em dropdown — nao pode vazar p/ radio.
        assert "is_multiple_dropdown" not in payload["extra_data"]

    async def test_delete_form_purges_via_query_flag(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.delete",
            new=AsyncMock(return_value={}),
        ) as delete:
            await form_service.delete_form(7, purge=True)
        delete.assert_awaited_once_with(f"/apirest.php/{FORM_ENC}/7?force_purge=true")

    async def test_delete_question_targets_question_itemtype(self) -> None:
        with patch(
            "src.services.form_service.glpi_client.delete",
            new=AsyncMock(return_value={}),
        ) as delete:
            await form_service.delete_question(5)
        delete.assert_awaited_once_with(f"/apirest.php/{QUESTION_ENC}/5?force_purge=true")

    async def test_get_form_embeds_sections(self) -> None:
        sections = [{"id": 3, "name": "S1"}, {"id": 4, "name": "S2"}]
        with patch(
            "src.services.form_service.glpi_client.get",
            new=AsyncMock(side_effect=[
                {"id": 42, "name": "Form"},   # GET form
                sections,                     # GET sections
                [],                           # questions of S1
                [],                           # comments of S1
                [],                           # questions of S2
                [],                           # comments of S2
            ]),
        ):
            result = await form_service.get_form(42)
        assert len(result["sections"]) == 2
        assert result["sections"][0]["questions"] == []

    async def test_glpi_rejects_encoded_itemtype_with_clear_message(self) -> None:
        """A 400 NOT_COMMONDBTM means the env, not the form, is the problem."""
        from src.services.glpi_client import glpi_client as client_module
        with patch.object(
            client_module, "get",
            new=AsyncMock(side_effect=GLPIError(
                400, "HTTP error: {\"ERROR_MESSAGE\":\"...\","
                "\"ERROR_CODE\":\"ERROR_RESOURCE_NOT_FOUND_NOR_COMMONDBTM\"}"
            )),
        ):
            with pytest.raises(GLPIError) as excinfo:
                await form_service.get_form(42)
        assert "webserver" in str(excinfo.value.message)


# ---------------------------------------------------------------------------
# Tool layer — validation and guard wiring
# ---------------------------------------------------------------------------


class TestSearchFormsTool:
    async def test_rejects_unknown_scope(self) -> None:
        result = await search_forms(scope="widgets")
        assert "scope" in str(result)

    async def test_rejects_short_query(self) -> None:
        with pytest.raises(ValidationError):
            await search_forms(scope="forms", query="a")

    async def test_renders_markdown_table(self) -> None:
        with patch(
            "src.tools.consolidated_forms.form_service.search_forms",
            new=AsyncMock(return_value={
                "scope": "forms",
                "items": [{"id": "1", "name": "Acesso VPN", "category": "TI",
                           "is_active": "1", "date_mod": "2026-08-01 10:00:00"}],
                "total": 1, "limit": 10, "offset": 0,
            }),
        ):
            text = await search_forms(scope="forms")
        assert "Acesso VPN" in text
        assert "Titulo" in text


class TestManageFormsTool:
    async def test_create_form_requires_name(self) -> None:
        result = await manage_forms(action="create")
        assert "obrigatorio" in str(result)

    async def test_create_form_defaults_is_active_true(self) -> None:
        """Sem is_active, o formulario nasce ativo (default True)."""
        with patch(
            "src.tools.consolidated_forms.form_service.create_form",
            new=AsyncMock(return_value={"id": 1, "name": "X"}),
        ) as create:
            await manage_forms(action="create", name="X")
        assert create.await_args.kwargs["is_active"] is True

    async def test_update_form_omits_unset_flags(self) -> None:
        """Atualizar sem is_active/is_pinned nao deve gravar 0 nesses campos."""
        with patch(
            "src.tools.consolidated_forms.form_service.update_form",
            new=AsyncMock(return_value={"id": 1, "name": "X"}),
        ) as update:
            await manage_forms(action="update", form_id=1, name="Y")
        kwargs = update.await_args.kwargs
        assert kwargs["is_active"] is None
        assert kwargs["is_pinned"] is None
        assert kwargs["is_draft"] is None
        assert kwargs["render_layout"] is None

    async def test_update_form_passes_render_layout(self) -> None:
        """update com render_layout=single_page chega ao servico normalizado."""
        with patch(
            "src.tools.consolidated_forms.form_service.update_form",
            new=AsyncMock(return_value={"id": 1, "name": "X"}),
        ) as update:
            await manage_forms(action="update", form_id=1, render_layout="pagina unica")
        assert update.await_args.kwargs["render_layout"] == "single_page"

    async def test_create_form_passes_render_layout(self) -> None:
        with patch(
            "src.tools.consolidated_forms.form_service.create_form",
            new=AsyncMock(return_value={"id": 1, "name": "X"}),
        ) as create:
            await manage_forms(action="create", name="X", render_layout="single_page")
        assert create.await_args.kwargs["render_layout"] == "single_page"

    async def test_update_form_refuses_unknown_render_layout(self) -> None:
        with patch(
            "src.tools.consolidated_forms.form_service.update_form",
            new=AsyncMock(return_value={"id": 1, "name": "X"}),
        ):
            with pytest.raises(ValidationError):
                await manage_forms(action="update", form_id=1, render_layout="carrossel")

    async def test_create_question_requires_type(self) -> None:
        result = await manage_forms(action="create_question", section_id=1, name="X")
        assert "obrigatorios" in str(result)

    async def test_delete_runs_through_safety_guard(self) -> None:
        with patch(
            "src.tools.consolidated_forms.require_safety_confirmation"
        ) as guard, patch(
            "src.tools.consolidated_forms.form_service.delete_form",
            new=AsyncMock(return_value={"id": 7, "purged": True}),
        ):
            await manage_forms(action="delete", form_id=7)

        guard.assert_called_once()
        assert guard.call_args.args[0] == "delete_form"

    async def test_guard_protected_operations_include_forms(self) -> None:
        protected = safety_guard.PROTECTED_OPERATIONS
        for op in ("delete_form", "delete_form_section", "delete_form_question",
                   "delete_form_comment", "delete_form_category"):
            assert op in protected

    def test_mutation_message_handles_delete_suffix(self) -> None:
        assert "purgado" in _format_mutation("delete_category", {"id": 1, "purged": True})
        assert "salvo" in _format_mutation("create", {"id": 1})


# ---------------------------------------------------------------------------
# Write policy wiring
# ---------------------------------------------------------------------------


class TestWritePolicyWiring:
    def test_form_operations_are_known(self) -> None:
        assert resolve_operation("forms", "create") is WriteOperation.FORM_CREATE
        assert resolve_operation("forms", "update") is WriteOperation.FORM_UPDATE
        assert resolve_operation("forms", "delete") is WriteOperation.FORM_DELETE
        assert resolve_operation("forms", "create_question") is WriteOperation.FORM_STRUCTURE_CREATE
        assert resolve_operation("forms", "delete_question") is WriteOperation.FORM_QUESTION_DELETE
        assert resolve_operation("forms", "create_category") is WriteOperation.FORM_CATEGORY_CREATE
        assert resolve_operation("forms", "delete_category") is WriteOperation.FORM_CATEGORY_DELETE

    def test_form_read_actions_are_not_writes(self) -> None:
        assert resolve_operation("forms", "get") is None
        assert resolve_operation("forms", "list_sections") is None

    def test_form_deletes_default_to_disabled(self) -> None:
        from src.security.write_policy import WRITE_OPERATIONS
        for op in (WriteOperation.FORM_DELETE, WriteOperation.FORM_SECTION_DELETE,
                   WriteOperation.FORM_QUESTION_DELETE, WriteOperation.FORM_COMMENT_DELETE,
                   WriteOperation.FORM_CATEGORY_DELETE):
            assert WRITE_OPERATIONS[op].default_enabled is False
            assert WRITE_OPERATIONS[op].destructive is True

    def test_form_deletes_link_to_safety_guard(self) -> None:
        from src.security.write_policy import WRITE_OPERATIONS
        assert WRITE_OPERATIONS[WriteOperation.FORM_DELETE].safety_guard_operation == "delete_form"
        assert (
            WRITE_OPERATIONS[WriteOperation.FORM_QUESTION_DELETE].safety_guard_operation
            == "delete_form_question"
        )
