import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Table } from "antd";
import type { Messages } from "./i18n";
import { TableScroll } from "./TableScroll";

type Pull = {
  id: number;
  status: string;
  started_at: string;
  tables: number;
  tasks: number;
  error: string | null;
};
export function PullsPanel({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [available, setAvailable] = useState(false);
  const [pulls, setPulls] = useState<Pull[]>([]);
  const [error, setError] = useState(false);
  const [running, setRunning] = useState(false);
  const refresh = useCallback(async () => {
    const response = await fetch("/api/v1/pulls", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error();
    setPulls((await response.json()) as Pull[]);
  }, [token]);
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/v1/plugins", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error();
        const plugins: unknown = await response.json();
        if (
          Array.isArray(plugins) &&
          plugins.some(
            (plugin) => plugin.category === "puller" && plugin.id === "dtb",
          )
        ) {
          setAvailable(true);
          await refresh();
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [token, refresh]);
  async function pull() {
    setRunning(true);
    try {
      const response = await fetch("/api/v1/pulls/dtb", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      await refresh();
      setError(false);
    } catch {
      setError(true);
    } finally {
      setRunning(false);
    }
  }
  if (!available) return error ? <Alert type="error" title={t.error} /> : null;
  return (
    <section className="stack">
      <div className="page-header">
        <div>
          <h2 className="page-title">{t.pullRecords}</h2>
        </div>
        <div className="page-actions">
          <Button disabled={running} onClick={() => void pull()}>
            {t.pullNow}
          </Button>
        </div>
      </div>
      {error && <Alert type="error" title={t.error} />}
      <div className="card token-table-card">
        <TableScroll label={t.pullRecords}>
          <Table
            rowKey="id"
            dataSource={pulls}
            columns={[
              { title: t.buildStartedAt, dataIndex: "started_at" },
              { title: t.buildStatus, dataIndex: "status" },
              { title: t.entityTable, dataIndex: "tables" },
              { title: t.entityTask, dataIndex: "tasks" },
              { title: t.pullFailure, dataIndex: "error" },
            ]}
          />
        </TableScroll>
      </div>
    </section>
  );
}
