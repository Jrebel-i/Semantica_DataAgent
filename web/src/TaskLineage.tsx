import { useEffect, useState } from "react";
import { Alert, Typography } from "antd";
import { ReactFlow, Position, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { components } from "./api";
import type { Messages } from "./i18n";

export type TableLineage = components["schemas"]["TableLineageView"];
type LineageEdge = TableLineage["upstream"][number];

function mergeEdges(
  edges: LineageEdge[],
): { source: string; target: string; records: LineageEdge[] }[] {
  const groups = new Map<
    string,
    { source: string; target: string; records: LineageEdge[] }
  >();
  for (const edge of edges) {
    const key = `${edge.upstream}->${edge.downstream}`;
    const group = groups.get(key) ?? {
      source: edge.upstream,
      target: edge.downstream,
      records: [],
    };
    group.records.push(edge);
    groups.set(key, group);
  }
  return [...groups.values()];
}

function edgeDescription(records: LineageEdge[]) {
  return records
    .map(
      (record) =>
        `${record.evidence} · ${record.parse_status} · ${record.task_id}`,
    )
    .join("；");
}

export function TaskLineage({
  table,
  token,
  messages,
  onSelect,
  onData,
  showTasks = true,
}: {
  table: string;
  token: string;
  messages: Messages;
  onSelect?: (name: string) => void;
  onData?: (data: TableLineage) => void;
  showTasks?: boolean;
}) {
  const [data, setData] = useState<TableLineage | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    fetch(`/api/v1/table-lineage?${new URLSearchParams({ table })}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json() as Promise<TableLineage>;
      })
      .then((result) => {
        setData(result);
        onData?.(result);
        setError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
    // onData is a stable state setter in the pages that need the raw lineage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table, token]);
  const upstream = data
    ? mergeEdges(
        data.upstream.map((edge) => ({
          ...edge,
          downstream: table,
        })),
      )
    : [];
  const downstream = data
    ? mergeEdges(
        data.downstream.map((edge) => ({
          ...edge,
          upstream: table,
        })),
      )
    : [];
  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      {data && (
        <>
          <LocalLineage
            table={table}
            upstream={upstream}
            downstream={downstream}
            onSelect={onSelect}
            label={messages.contextLineage}
            empty={messages.emptyContext}
          />
          {upstream.length + downstream.length > 0 && (
            <ul
              className="lineage-edge-list"
              aria-label={messages.contextLineage}
            >
              {[...upstream, ...downstream].map((edge) => (
                <li
                  className="lineage-edge"
                  key={`${edge.source}->${edge.target}`}
                >
                  <span className="lineage-edge-text">
                    {edge.source} → {edge.target} ·{" "}
                    {edgeDescription(edge.records)}
                  </span>
                  {edge.records.length > 1 && (
                    <span className="subtle">{edge.records.length} 条记录</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {showTasks && data.tasks.length > 0 && (
            <section>
              <Typography.Title level={4}>
                {messages.producingTasks}
              </Typography.Title>
              <ul>
                {data.tasks.map((task) => (
                  <li key={task.ref}>{task.name}</li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </>
  );
}

function LocalLineage({
  table,
  upstream,
  downstream,
  onSelect,
  label,
  empty,
}: {
  table: string;
  upstream: { source: string; target: string; records: LineageEdge[] }[];
  downstream: { source: string; target: string; records: LineageEdge[] }[];
  onSelect?: (name: string) => void;
  label: string;
  empty: string;
}) {
  const sourceNames = [...new Set(upstream.map((edge) => edge.source))].filter(
    (name) => name !== table,
  );
  const targetNames = [
    ...new Set(downstream.map((edge) => edge.target)),
  ].filter((name) => name !== table && !sourceNames.includes(name));
  if (sourceNames.length === 0 && targetNames.length === 0)
    return <div className="lineage-empty">{empty}</div>;
  const max = Math.max(sourceNames.length, targetNames.length, 1);
  const canvasHeight = Math.max(200, Math.min(420, 120 + max * 48));
  const centerY = ((max - 1) / 2) * 74;
  const nodes: Node[] = [
    ...sourceNames.map((name, index) => ({
      id: name,
      position: { x: 0, y: index * 74 },
      data: { label: name },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    })),
    {
      id: table,
      position: { x: 320, y: centerY },
      data: { label: table },
      className: "current",
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    },
    ...targetNames.map((name, index) => ({
      id: name,
      position: { x: 640, y: index * 74 },
      data: { label: name },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    })),
  ];
  const edges: Edge[] = [
    ...upstream.map((edge) => ({
      id: `${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      type: "smoothstep",
      label: edge.records.length > 1 ? `${edge.records.length} 条` : undefined,
      labelBgPadding: [4, 2] as [number, number],
      labelBgStyle: { fill: "#F7F9F8" },
      data: { records: edge.records },
    })),
    ...downstream.map((edge) => ({
      id: `${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      type: "smoothstep",
      label: edge.records.length > 1 ? `${edge.records.length} 条` : undefined,
      labelBgPadding: [4, 2] as [number, number],
      labelBgStyle: { fill: "#F7F9F8" },
      data: { records: edge.records },
    })),
  ];
  return (
    <div
      className="lineage-canvas"
      aria-label={label}
      style={{ height: canvasHeight }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        nodesDraggable={false}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          if (node.id !== table) onSelect?.(node.id);
        }}
      />
    </div>
  );
}
