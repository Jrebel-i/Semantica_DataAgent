import { useEffect, useState } from "react";
import { Alert, Typography } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type Lineage = components["schemas"]["TableLineageView"];

export function TaskLineage({
  table,
  token,
  messages,
}: {
  table: string;
  token: string;
  messages: Messages;
}) {
  const [data, setData] = useState<Lineage | null>(null);
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
        return r.json() as Promise<Lineage>;
      })
      .then((result) => {
        setData(result);
        setError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [table, token]);
  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      {data && (
        <>
          <Typography.Title level={4}>{messages.upstream}</Typography.Title>
          <ul>
            {data.upstream.map((edge) => (
              <li key={edge.ref}>
                {edge.upstream} → {edge.downstream} ({edge.evidence},{" "}
                {edge.parse_status}, {edge.task_id})
              </li>
            ))}
          </ul>
          <Typography.Title level={4}>{messages.downstream}</Typography.Title>
          <ul>
            {data.downstream.map((edge) => (
              <li key={edge.ref}>
                {edge.upstream} → {edge.downstream} ({edge.evidence},{" "}
                {edge.parse_status}, {edge.task_id})
              </li>
            ))}
          </ul>
          <Typography.Title level={4}>
            {messages.producingTasks}
          </Typography.Title>
          <ul>
            {data.tasks.map((task) => (
              <li key={task.ref}>{task.name}</li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
