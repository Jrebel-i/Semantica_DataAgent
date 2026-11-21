import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Table, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { TableScroll } from "./TableScroll";

type Pull = components["schemas"]["PullView"];
type ProjectAdapter = components["schemas"]["ProjectAdapterView"];

export function PullsPanel({
  token,
  project,
  messages: t,
}: {
  token: string;
  project?: string;
  messages: Messages;
}) {
  const [available, setAvailable] = useState(false);
  const [pullerId, setPullerId] = useState<"dtb" | "ossie">("dtb");
  const [pulls, setPulls] = useState<Pull[]>([]);
  const [error, setError] = useState(false);
  const [running, setRunning] = useState(false);
  const generation = useRef(0);

  const pullsUrl = project
    ? `/api/v1/pulls?project=${encodeURIComponent(project)}`
    : "/api/v1/pulls";

  const refresh = useCallback(
    async (signal?: AbortSignal, expectedGen = generation.current) => {
      const response = await fetch(pullsUrl, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      if (!response.ok) throw new Error();
      const rows = (await response.json()) as Pull[];
      if (signal?.aborted || generation.current !== expectedGen) return;
      setPulls(rows);
    },
    [pullsUrl, token],
  );

  useEffect(() => {
    const controller = new AbortController();
    const expectedGen = generation.current + 1;
    generation.current = expectedGen;
    setAvailable(false);
    setPulls([]);
    setError(false);
    setRunning(false);

    if (project) {
      void fetch(`/api/v1/projects/${encodeURIComponent(project)}/adapters`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!response.ok) throw new Error();
          const adapters: unknown = await response.json();
          if (controller.signal.aborted || generation.current !== expectedGen)
            return;
          const pullAdapters = Array.isArray(adapters)
            ? (adapters as ProjectAdapter[]).filter(
                (row) =>
                  row.compatible &&
                  row.pull &&
                  (row.id === "dtb" || row.id === "ossie"),
              )
            : [];
          if (!pullAdapters.length) return;
          const ossieRow = pullAdapters.find((row) => row.id === "ossie");
          const dtbRow = pullAdapters.find((row) => row.id === "dtb");
          const useOssie = Boolean(
            ossieRow?.pull?.enabled ||
            (!dtbRow?.pull?.enabled && ossieRow?.enabled),
          );
          setPullerId(useOssie ? "ossie" : "dtb");
          setAvailable(true);
          await refresh(controller.signal, expectedGen);
        })
        .catch(() => {
          if (!controller.signal.aborted && generation.current === expectedGen)
            setError(true);
        });
    } else {
      void fetch("/api/v1/plugins", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!response.ok) throw new Error();
          const plugins: unknown = await response.json();
          if (controller.signal.aborted || generation.current !== expectedGen)
            return;
          if (
            Array.isArray(plugins) &&
            plugins.some(
              (plugin) =>
                plugin.category === "puller" &&
                (plugin.id === "dtb" || plugin.id === "ossie"),
            )
          ) {
            const ossieEnabled = plugins.some(
              (plugin) =>
                plugin.category === "puller" &&
                plugin.id === "ossie" &&
                plugin.enabled === true,
            );
            setPullerId(ossieEnabled ? "ossie" : "dtb");
            setAvailable(true);
            await refresh(controller.signal, expectedGen);
          }
        })
        .catch(() => {
          if (!controller.signal.aborted && generation.current === expectedGen)
            setError(true);
        });
    }
    return () => controller.abort();
  }, [project, token, refresh]);

  async function pull() {
    const expectedGen = generation.current;
    setRunning(true);
    try {
      const targetUrl = project
        ? `/api/v1/pulls/${pullerId}?project=${encodeURIComponent(project)}`
        : `/api/v1/pulls/${pullerId}`;
      const response = await fetch(targetUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      if (generation.current !== expectedGen) return;
      await refresh(undefined, expectedGen);
      if (generation.current === expectedGen) setError(false);
    } catch {
      if (generation.current === expectedGen) setError(true);
    } finally {
      if (generation.current === expectedGen) setRunning(false);
    }
  }
  if (!available) return error ? <Alert type="error" title={t.error} /> : null;
  return (
    <section className="stack">
      <div className="page-header">
        <Space align="center" wrap>
          <h2 className="page-title">{t.pullRecords}</h2>
          <Tag>{pullerId === "ossie" ? t.adapterPullOssie : t.adapterPull}</Tag>
        </Space>
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
