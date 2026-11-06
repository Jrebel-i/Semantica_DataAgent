import { useEffect, useState } from "react";
import { Alert, List, Typography } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

export function BuildsPage({
  token,
  messages,
}: {
  token: string;
  messages: Messages;
}) {
  const [builds, setBuilds] = useState<components["schemas"]["BuildView"][]>(
    [],
  );
  const [error, setError] = useState(false);
  const [pulls, setPulls] = useState<components["schemas"]["PullView"][]>([]);
  const [pulling, setPulling] = useState(false);
  const refreshPulls = () =>
    fetch("/api/v1/pulls", { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json();
      })
      .then(setPulls)
      .catch(() => setError(true));
  useEffect(() => {
    void refreshPulls();
    const controller = new AbortController();
    fetch("/api/v1/builds", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json() as Promise<components["schemas"]["BuildView"][]>;
      })
      .then(setBuilds)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [token]);
  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      <Typography.Title level={3}>{messages.pulls}</Typography.Title>
      <button
        disabled={pulling}
        onClick={() => {
          setPulling(true);
          fetch("/api/v1/pulls/dtb", {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          })
            .then((r) => {
              if (!r.ok) throw new Error();
              return refreshPulls();
            })
            .catch(() => setError(true))
            .finally(() => setPulling(false));
        }}
      >
        {messages.pullNow}
      </button>
      <List
        dataSource={pulls}
        renderItem={(pull) => (
          <List.Item>
            <div>
              {pull.started_at} {pull.status} · {messages.pullTables}:{" "}
              {pull.tables} · {messages.pullTasks}: {pull.tasks} {pull.error}
            </div>
          </List.Item>
        )}
      />
      <List
        dataSource={builds}
        renderItem={(build) => (
          <List.Item>
            <div>
              <Typography.Title level={4}>
                #{build.id} {build.status}
              </Typography.Title>
              <div>
                {build.started_at} {build.error}
              </div>
              <div>
                {messages.embeddingStats}: {messages.embeddingRequests}{" "}
                {build.counts.embedding_requests ?? 0},{" "}
                {messages.embeddingBatches}{" "}
                {build.counts.embedding_successful_batches ?? 0},{" "}
                {messages.embeddingRetriesCount}{" "}
                {build.counts.embedding_retries ?? 0},{" "}
                {messages.embeddingElapsed}{" "}
                {build.counts.embedding_elapsed_ms ?? 0}
              </div>
              <div>{messages.parseFailures}</div>
              <ul>
                {build.parse_failures.map((failure, index) => (
                  <li key={index}>
                    {failure.task_id}: {failure.reason}
                  </li>
                ))}
              </ul>
            </div>
          </List.Item>
        )}
      />
    </>
  );
}
