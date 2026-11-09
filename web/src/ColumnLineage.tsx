import { useEffect, useState } from "react";
import { Alert } from "antd";
import { ReactFlow, Position, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type RecordEdge = {
  upstream_table: string;
  upstream_field: string;
  downstream_table: string;
  downstream_field: string;
  task_id: string;
  task_type: string | null;
  evidence: string;
  parse_status: string;
};

type Data = { upstream: RecordEdge[]; downstream: RecordEdge[] };

export function ColumnLineage({
  table,
  column,
  token,
  project,
}: {
  table: string;
  column: string;
  token: string;
  project?: string;
}) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(false);
    const params = new URLSearchParams({ table, column, depth: "2" });
    if (project) params.set("project", project);
    fetch(`/api/v1/column-lineage?${params}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Data>;
      })
      .then(setData)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [project, table, column, token]);
  if (error) return <Alert type="error" title="字段血缘加载失败" />;
  if (!data) return null;
  const records = [
    ...new Map(
      [...data.upstream, ...data.downstream].map((r) => [JSON.stringify(r), r]),
    ).values(),
  ];
  if (!records.length)
    return (
      <div className="lineage-empty">暂无字段级血缘，任务可能未能解析。</div>
    );
  const current = `${table}.${column}`;
  const source = (r: RecordEdge) => `${r.upstream_table}.${r.upstream_field}`;
  const target = (r: RecordEdge) =>
    `${r.downstream_table}.${r.downstream_field}`;
  const levels = new Map<string, number>([[current, 0]]);
  for (let i = 0; i < 2; i++) {
    for (const r of data.upstream) {
      const level = levels.get(target(r));
      if (level !== undefined && !levels.has(source(r)))
        levels.set(source(r), level - 1);
    }
    for (const r of data.downstream) {
      const level = levels.get(source(r));
      if (level !== undefined && !levels.has(target(r)))
        levels.set(target(r), level + 1);
    }
  }
  const offsets = new Map<number, number>();
  const nodes: Node[] = [...levels].map(([name, level]) => {
    const offset = offsets.get(level) ?? 0;
    offsets.set(level, offset + 1);
    return {
      id: name,
      position: { x: level * 320, y: offset * 80 },
      data: { label: name },
      className: name === current ? "current" : "",
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    };
  });
  const edges: Edge[] = records.map((r, i) => ({
    id: `${i}`,
    source: `${r.upstream_table}.${r.upstream_field}`,
    target: `${r.downstream_table}.${r.downstream_field}`,
    type: "smoothstep",
    label: r.task_id,
  }));
  return (
    <>
      <div
        className="lineage-canvas"
        aria-label="字段级血缘"
        style={{ height: 300 }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          nodesDraggable={false}
          nodesConnectable={false}
          proOptions={{ hideAttribution: true }}
        />
      </div>
      <ul className="lineage-edge-list" aria-label="字段血缘明细">
        {records.map((r, i) => (
          <li className="lineage-edge" key={i}>
            {source(r)} → {target(r)} · {r.task_id} ({r.task_type ?? "—"}) ·{" "}
            {r.evidence} · {r.parse_status}
          </li>
        ))}
      </ul>
    </>
  );
}
