"""Table knowledge REST endpoints and MCP tools."""

from typing import Annotated, ClassVar, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from . import knowledge
from .auth import current_user
from .project_api import project_scope
from .project_context import DEFAULT_PROJECT_ID, ProjectContext

router = APIRouter()


class FieldView(BaseModel):
    name: str
    type: str
    comment: str
    partition: bool
    synonyms: list[str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    instructions: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    dimension_count: int | None = None
    dimension_too_many: bool | None = None
    dimension_samples: list[str] | None = None


class _OmitOptionalResponseFields(BaseModel):
    omit_when_none: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def serialize_optional_fields(self, handler: SerializerFunctionWrapHandler):
        result = handler(self)
        return {
            key: value
            for key, value in result.items()
            if key not in self.omit_when_none or value is not None
        }


class DocumentLink(_OmitOptionalResponseFields):
    model_config = ConfigDict(exclude_none=True)
    omit_when_none = frozenset({"generated"})

    ref: str
    path: str
    category: str
    generated: bool | None = None


class RelationshipLink(BaseModel):
    name: str
    ref: str
    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    description: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    instructions: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TableSummary(_OmitOptionalResponseFields):
    model_config = ConfigDict(exclude_none=True)
    omit_when_none = frozenset({"catalog"})

    catalog: str | None = None
    name: str
    database: str
    comment: str
    ref: str
    field_count: int
    document_count: int
    has_partition: bool


class TableView(_OmitOptionalResponseFields):
    model_config = ConfigDict(exclude_none=True)
    omit_when_none = frozenset({"catalog", "relationships"})

    catalog: str | None = None
    name: str
    database: str
    comment: str
    ref: str
    synonyms: list[str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    instructions: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    fields: list[FieldView]
    documents: list[DocumentLink]
    relationships: list[RelationshipLink] | None = None


@router.get("/api/v1/tables", response_model=list[TableView | TableSummary])
def tables(
    _: Annotated[str, Depends(current_user)],
    context: Annotated[ProjectContext, Depends(project_scope)],
    database: str | None = None,
    search: str | None = None,
    view: Literal["summary", "full"] = "full",
):
    arguments = {
        "database": database,
        "search": search,
        "summary": view == "summary",
    }
    if context.project_id != DEFAULT_PROJECT_ID:
        arguments["project_id"] = context.project_id
    return knowledge.describe(**arguments)


@router.get("/api/v1/tables/{database}/{table}", response_model=TableView)
def table_detail(
    _: Annotated[str, Depends(current_user)],
    context: Annotated[ProjectContext, Depends(project_scope)],
    database: str,
    table: str,
):
    arguments = {"database": database, "names": [f"{database}.{table}"]}
    if context.project_id != DEFAULT_PROJECT_ID:
        arguments["project_id"] = context.project_id
    rows = knowledge.describe(**arguments)
    match = next(
        (
            row
            for row in rows
            if row.get("database") == database and row.get("name") == table
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Table not found")
    return match


def describe_tables(names: list[str], project_id: str | None = None) -> list[dict]:
    if project_id in (None, DEFAULT_PROJECT_ID):
        return knowledge.describe(names=names)
    return knowledge.describe(names=names, project_id=project_id)


def search_knowledge(
    query: str,
    scope: Literal["tables", "fields", "docs", "tasks", "all"],
    top_k: int = 5,
    category: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    if not query or not 1 <= top_k <= 100:
        raise ValueError("Invalid query or top_k")
    if category is not None and scope not in ("docs", "all"):
        raise ValueError("category requires docs scope")
    if project_id in (None, DEFAULT_PROJECT_ID):
        return knowledge.search(query, scope, top_k, category)
    return knowledge.search(query, scope, top_k, category, project_id=project_id)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="allow")

    ref: str
    type: str
    score: float
    sources: list[Literal["exact", "fulltext", "vector", "graph"]]
    confidence: Literal["high", "medium", "low"]
    ambiguous: bool
    ambiguity_refs: list[str]
    name: str | None = None
    content: str | None = None
    comment: str | None = None
    description: str | None = None
    path: str | None = None


@router.get(
    "/api/v1/search", response_model=list[SearchHit], response_model_exclude_unset=True
)
def search_route(
    _: Annotated[str, Depends(current_user)],
    context: Annotated[ProjectContext, Depends(project_scope)],
    query: Annotated[str, Query(min_length=1)],
    scope: Literal["tables", "fields", "docs", "tasks", "all"] = "all",
    top_k: Annotated[int, Query(ge=1, le=100)] = 5,
    category: str | None = None,
):
    try:
        return search_knowledge(
            query, scope, top_k, category, project_id=context.project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
