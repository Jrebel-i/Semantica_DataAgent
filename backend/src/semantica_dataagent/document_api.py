"""Knowledge category and document browsing for REST and MCP."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import knowledge
from .app import current_user

router = APIRouter()


class DocumentSummary(BaseModel):
    ref: str
    path: str


class CategoryView(BaseModel):
    ref: str
    name: str
    description: str
    documents: list[DocumentSummary]


class DocumentView(BaseModel):
    ref: str
    category: str
    path: str
    content: str
    start_line: int
    end_line: int
    generated: bool


def list_categories() -> list[dict]:
    values = knowledge.settings()
    rows = knowledge.execute(
        knowledge.read_graph(values),
        """MATCH (c:SDACategory {generation:$generation})
        OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(d:SDADocument)
        RETURN c.name AS name, c.ref AS ref, collect(properties(d)) AS docs ORDER BY name""",
        {"generation": values["knowledge_generation"]},
    )
    result = []
    for row in rows:
        docs = [d for d in row["docs"] if d]
        index = next((d for d in docs if d["path"] == "index.md"), {})
        description = next(
            (
                line.strip()
                for line in index.get("content", "").splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )
        result.append(
            {
                "name": row["name"],
                "ref": row["ref"],
                "description": description,
                "documents": [
                    {"ref": d["ref"], "path": d["path"]}
                    for d in docs
                    if d["path"] != "index.md"
                ],
            }
        )
    return result


def read_document(doc_id: str, section: str | None = None) -> dict:
    values = knowledge.settings()
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (d:SDADocument {generation:$generation, ref:$ref}) RETURN properties(d) AS doc",
        {"generation": values["knowledge_generation"], "ref": doc_id},
    )
    if not rows:
        raise ValueError("Document not found")
    doc = rows[0]["doc"]
    lines = doc["content"].splitlines()
    start, end = 0, len(lines)
    if section is not None:
        from .markdown import section_range

        start, end = section_range(lines, section)
    return {
        "ref": doc["ref"],
        "category": doc["category"],
        "path": doc["path"],
        "content": "\n".join(lines[start:end]),
        "start_line": start + 1,
        "end_line": end,
        "generated": doc.get("generated", False),
    }


def read_category_index(category: str) -> dict:
    from .markdown import doc_ref

    return read_document(doc_ref(category, "index.md"))


@router.get("/api/v1/categories", response_model=list[CategoryView])
def categories_route(_: Annotated[str, Depends(current_user)]):
    return list_categories()


@router.get("/api/v1/category-index", response_model=DocumentView)
def index_route(_: Annotated[str, Depends(current_user)], category: str):
    try:
        return read_category_index(category)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/document", response_model=DocumentView)
def document_route(
    _: Annotated[str, Depends(current_user)], doc_id: str, section: str | None = None
):
    try:
        return read_document(doc_id, section)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
