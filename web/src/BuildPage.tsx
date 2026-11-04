import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type Build = components["schemas"]["BuildView"];
type BuildType = Build["build_type"];
type BuildStatus = Build["status"];

const statusColors: Record<BuildStatus, string> = {
  queued: "gold",
  running: "blue",
  succeeded: "green",
  failed: "red",
  cancelled: "default",
};

const countLabels: Record<string, keyof Messages> = {
  added: "fileAdded",
  changed: "fileChanged",
  deleted: "fileDeleted",
  filtered: "filtered",
  table: "entityTable",
  field: "entityField",
  task: "entityTask",
  lineage: "entityLineage",
  dimension_value: "entityDimensionValue",
  document: "entityDocument",
  chunk: "entityChunk",
  category: "entityCategory",
};

function displayTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

export function BuildPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [builds, setBuilds] = useState<Build[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    const response = await fetch("/api/v1/builds", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error();
    setBuilds((await response.json()) as Build[]);
  }, [token]);
  useEffect(() => {
    void load().catch(() => setError(t.buildError));
    const timer = window.setInterval(() => {
      void load().catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [load, t.buildError]);
  useEffect(() => {
    if (selectedId === null && builds.length) setSelectedId(builds[0].id);
  }, [builds, selectedId]);
  async function trigger(buildType: BuildType) {
    try {
      const response = await fetch("/api/v1/builds", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ build_type: buildType }),
      });
      if (!response.ok) throw new Error();
      const created = (await response.json()) as Build;
      setError("");
      setNotice(t.buildTriggered);
      setSelectedId(created.id);
      await load();
    } catch {
      setNotice("");
      setError(t.buildError);
    }
  }
  const selected = builds.find((build) => build.id === selectedId) ?? null;
  return (
    <section>
      <Typography.Title level={2}>{t.builds}</Typography.Title>
      <Space>
        <Button type="primary" onClick={() => void trigger("full")}>
          {t.buildFull}
        </Button>
        <Button onClick={() => void trigger("incremental")}>
          {t.buildIncremental}
        </Button>
      </Space>
      {notice && <Alert type="success" title={notice} />}
      {error && <Alert type="error" title={error} />}
      <Table
        rowKey="id"
        dataSource={builds}
        locale={{ emptyText: t.noBuilds }}
        pagination={false}
        onRow={(row) => ({ onClick: () => setSelectedId(row.id) })}
        columns={[
          { title: t.buildId, dataIndex: "id" },
          {
            title: t.buildStatus,
            render: (_, row) => (
              <Tag color={statusColors[row.status]}>{t[row.status]}</Tag>
            ),
          },
          {
            title: t.buildType,
            render: (_, row) => t[row.build_type],
          },
          { title: t.buildTriggeredBy, dataIndex: "triggered_by" },
          {
            title: t.buildStartedAt,
            render: (_, row) => displayTime(row.started_at),
          },
          {
            title: t.buildFinishedAt,
            render: (_, row) => displayTime(row.finished_at),
          },
        ]}
      />
      {!selected && <p>{t.buildSelectHint}</p>}
      {selected && (
        <>
          <Descriptions
            title={`${t.builds} #${selected.id}`}
            column={2}
            items={[
              {
                key: "status",
                label: t.buildStatus,
                children: t[selected.status],
              },
              {
                key: "type",
                label: t.buildType,
                children: t[selected.build_type],
              },
              {
                key: "triggered",
                label: t.buildTriggeredBy,
                children: selected.triggered_by ?? "—",
              },
              {
                key: "commit",
                label: t.buildSourceCommit,
                children: selected.source_commit ?? "—",
              },
              {
                key: "started",
                label: t.buildStartedAt,
                children: displayTime(selected.started_at),
              },
              {
                key: "finished",
                label: t.buildFinishedAt,
                children: displayTime(selected.finished_at),
              },
              ...(selected.error
                ? [
                    {
                      key: "error",
                      label: t.failed,
                      children: selected.error,
                      span: 2,
                    },
                  ]
                : []),
            ]}
          />
          <Typography.Title level={3}>{t.buildStats}</Typography.Title>
          <Table
            rowKey="key"
            pagination={false}
            dataSource={Object.entries(selected.counts)
              .sort(([left], [right]) => left.localeCompare(right))
              .map(([key, value]) => ({ key, value }))}
            columns={[
              {
                title: t.buildStats,
                render: (_, row) =>
                  countLabels[row.key] ? t[countLabels[row.key]] : row.key,
              },
              { title: "", dataIndex: "value" },
            ]}
          />
          <Typography.Title level={3}>{t.buildLogs}</Typography.Title>
          <Table
            rowKey="index"
            pagination={false}
            dataSource={selected.logs.map((log, index) => ({ ...log, index }))}
            columns={[
              {
                title: t.buildLogTime,
                render: (_, row) => displayTime(row.timestamp),
              },
              { title: t.buildStage, dataIndex: "stage" },
              {
                title: t.buildLogs,
                render: (_, row) => (
                  <span
                    style={{
                      color: row.level === "error" ? "#cf1322" : undefined,
                    }}
                  >
                    {row.message}
                  </span>
                ),
              },
            ]}
          />
        </>
      )}
    </section>
  );
}
