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
}: {
  table: string;
  column: string;
  token: string;
}) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(false);
    fetch(
      `/api/v1/column-lineage?${new URLSearchParams({ table, column, depth: "2" })}`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      },
    )
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Data>;
      })
      .then(setData)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [table, column, token]);
  if (error) return <Alert type="error" title="字段血缘加载失败" />;
  if (!data) return null;
  const records = [...data.upstream, ...data.downstream];
  if (!records.length)
    return (
      <div className="lineage-empty">暂无字段级血缘，任务可能未能解析。</div>
    );
  const current = `${table}.${column}`;
  const names = [
    ...new Set(
      records.flatMap((r) => [
        `${r.upstream_table}.${r.upstream_field}`,
        `${r.downstream_table}.${r.downstream_field}`,
      ]),
    ),
  ];
  const nodes: Node[] = names.map((name, i) => ({
    id: name,
    position: {
      x:
        name === current
          ? 320
          : data.upstream.some(
                (r) => `${r.upstream_table}.${r.upstream_field}` === name,
              )
            ? 0
            : 640,
      y: i * 80,
    },
    data: { label: name },
    className: name === current ? "current" : "",
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
  }));
  const edges: Edge[] = records.map((r, i) => ({
    id: `${i}`,
    source: `${r.upstream_table}.${r.upstream_field}`,
    target: `${r.downstream_table}.${r.downstream_field}`,
    type: "smoothstep",
    label: `${r.task_id} (${r.task_type ?? "—"}) · ${r.evidence} · ${r.parse_status}`,
  }));
  return (
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
  );
}
