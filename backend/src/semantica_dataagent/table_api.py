"""Table knowledge REST endpoints and MCP tools."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import knowledge
from .auth import current_user

router = APIRouter()


class FieldView(BaseModel):
    name: str
    type: str
    comment: str
    partition: bool
    dimension_count: int | None = None
    dimension_too_many: bool | None = None
    dimension_samples: list[str] | None = None


class DocumentLink(BaseModel):
    ref: str
    path: str
    category: str


class TableView(BaseModel):
    name: str
    database: str
    comment: str
    ref: str
    fields: list[FieldView]
    documents: list[DocumentLink]


@router.get("/api/v1/tables", response_model=list[TableView])
def tables(
    _: Annotated[str, Depends(current_user)],
    database: str | None = None,
    search: str | None = None,
):
    return knowledge.describe(database=database, search=search)


def describe_tables(names: list[str]) -> list[dict]:
    return knowledge.describe(names=names)


def search_knowledge(
    query: str,
    scope: Literal["tables", "fields", "docs", "tasks", "all"],
    top_k: int = 5,
    category: str | None = None,
) -> list[dict]:
    if not query or not 1 <= top_k <= 100:
        raise ValueError("Invalid query or top_k")
    if category is not None and scope not in ("docs", "all"):
        raise ValueError("category requires docs scope")
    return knowledge.search(query, scope, top_k, category)


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
    query: Annotated[str, Query(min_length=1)],
    scope: Literal["tables", "fields", "docs", "tasks", "all"] = "all",
    top_k: Annotated[int, Query(ge=1, le=100)] = 5,
    category: str | None = None,
):
    try:
        return search_knowledge(query, scope, top_k, category)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
