"""Table knowledge REST endpoints and MCP tools."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import knowledge
from .app import current_user

router = APIRouter()


class FieldView(BaseModel):
    name: str
    type: str
    comment: str
    partition: bool


class TableView(BaseModel):
    name: str
    database: str
    comment: str
    ref: str
    fields: list[FieldView]


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
    query: str, scope: Literal["tables", "fields"], top_k: int = 5
) -> list[dict]:
    if not query or not 1 <= top_k <= 100:
        raise ValueError("Invalid query or top_k")
    return knowledge.search(query, scope, top_k)
