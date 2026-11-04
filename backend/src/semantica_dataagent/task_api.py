"""Task SQL and lineage access for MCP and table details."""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import knowledge
from .app import current_user

router = APIRouter()


def get_lineage(
    table: str, direction: Literal["up", "down"] = "up", depth: int = 1
) -> list[dict]:
    if not table or direction not in ("up", "down") or not 1 <= depth <= 3:
        raise ValueError("Invalid table, direction or depth")
    values = knowledge.settings()
    pattern = (
        "(start)<-[:DEPENDS_ON*1..%d]-(other)"
        if direction == "up"
        else "(start)-[:DEPENDS_ON*1..%d]->(other)"
    )
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (start:SDALineageTable {generation:$generation, name:$table}) MATCH p="
        + (pattern % depth)
        + " UNWIND relationships(p) AS edge RETURN DISTINCT properties(edge) AS edge",
        {"generation": values["knowledge_generation"], "table": table},
    )
    return [
        {
            **r["edge"],
            "ref": "lineage:"
            + quote(r["edge"]["task_id"], safe="")
            + ":"
            + quote(r["edge"]["upstream"], safe="")
            + ":"
            + quote(r["edge"]["downstream"], safe=""),
        }
        for r in rows
    ]


def read_task_sql(task_id: str) -> dict:
    values = knowledge.settings()
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (t:SDATask {generation:$generation, name:$name}) RETURN properties(t) AS task",
        {"generation": values["knowledge_generation"], "name": task_id},
    )
    if not rows:
        raise ValueError("Task not found")
    return rows[0]["task"]


class EdgeView(BaseModel):
    upstream: str
    downstream: str
    evidence: str
    parse_status: str
    task_id: str
    ref: str


class TaskView(BaseModel):
    name: str
    ref: str


class TableLineageView(BaseModel):
    upstream: list[EdgeView]
    downstream: list[EdgeView]
    tasks: list[TaskView]


@router.get("/api/v1/table-lineage", response_model=TableLineageView)
def table_lineage(table: str, _: Annotated[str, Depends(current_user)]):
    values = knowledge.settings()
    rows = knowledge.execute(
        knowledge.read_graph(values),
        "MATCH (t:SDATask {generation:$generation})-[:PRODUCES]->(:SDALineageTable {generation:$generation, name:$table}) RETURN t.name AS name, t.ref AS ref",
        {"generation": values["knowledge_generation"], "table": table},
    )
    return {
        "upstream": get_lineage(table, "up"),
        "downstream": get_lineage(table, "down"),
        "tasks": rows,
    }
