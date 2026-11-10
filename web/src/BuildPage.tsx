import { PullsPanel } from "./PullsPanel";
import { ProjectSelector, storedProject } from "./ProjectSelector";
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Descriptions, Input, Table, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { TableScroll } from "./TableScroll";

type Build = components["schemas"]["BuildView"];
type Generations = components["schemas"]["GenerationsView"];
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
  const [project, setProject] = useState(storedProject);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [generations, setGenerations] = useState<Generations | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [interval, setInterval] = useState("0");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const projectQuery = `project=${encodeURIComponent(project)}`;
  const load = useCallback(async () => {
    const response = await fetch(`/api/v1/builds?${projectQuery}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error();
    setBuilds((await response.json()) as Build[]);
    const versions = await fetch(`/api/v1/generations?${projectQuery}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (versions.ok) setGenerations((await versions.json()) as Generations);
  }, [project, projectQuery, token]);
  useEffect(() => {
    let active = true;
    void fetch(`/api/v1/projects/${encodeURIComponent(project)}/schedule`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return;
        const saved = (await response.json()) as { interval_seconds: number };
        if (active) setInterval(String(saved.interval_seconds));
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [project, token]);
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
  useEffect(() => {
    setSelectedId(null);
  }, [project]);
  async function trigger(buildType: BuildType) {
    try {
      const response = await fetch(`/api/v1/builds?${projectQuery}`, {
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
  async function switchGeneration(name: string) {
    try {
      const response = await fetch(
        `/api/v1/generations/${encodeURIComponent(name)}/switch?${projectQuery}`,
        { method: "POST", headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      await load();
    } catch {
      setError(t.buildError);
    }
  }
  async function saveSchedule() {
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(project)}/schedule`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ interval_seconds: Number(interval) }),
        },
      );
      if (!response.ok) throw new Error();
      const saved = (await response.json()) as { interval_seconds: number };
      setInterval(String(saved.interval_seconds));
      setNotice(t.scheduleSaved);
      setError("");
    } catch {
      setNotice("");
      setError(t.buildError);
    }
  }
  const selected = builds.find((build) => build.id === selectedId) ?? null;
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.builds}</h1>
          <p className="page-subtitle">{t.buildsHint}</p>
        </div>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
        />
      </div>
      <PullsPanel token={token} messages={t} />
      {generations?.behind && (
        <Alert
          type="warning"
          title={t.generationBehind}
          action={
            <Button onClick={() => void trigger("full")}>{t.buildFull}</Button>
          }
        />
      )}
      {notice && <Alert type="success" title={notice} />}
      {error && <Alert type="error" title={error} />}
      <div className="build-grid">
        <div className="build-stack">
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.scheduleTitle}</h2>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="build-schedule-interval">
                {t.scheduleInterval}
              </label>
              <Input
                id="build-schedule-interval"
                aria-label={t.scheduleInterval}
                type="number"
                min={0}
                max={604800}
                value={interval}
                onChange={(event) => setInterval(event.target.value)}
              />
              <p className="field-hint">{t.scheduleIntervalHint}</p>
            </div>
            <div className="save-bar">
              <Button onClick={() => void saveSchedule()}>
                {t.scheduleSave}
              </Button>
            </div>
          </section>
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.builds}</h2>
              <div className="build-toolbar">
                <Button type="primary" onClick={() => void trigger("full")}>
                  {t.buildFull}
                </Button>
                <Button onClick={() => void trigger("incremental")}>
                  {t.buildIncremental}
                </Button>
              </div>
            </div>
            <TableScroll label={t.builds}>
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
                      <Tag color={statusColors[row.status]}>
                        {t[row.status]}
                      </Tag>
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
            </TableScroll>
          </section>
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.generations}</h2>
            </div>
            <TableScroll label={t.generations}>
              <Table
                rowKey="name"
                pagination={false}
                dataSource={generations?.generations ?? []}
                columns={[
                  { title: t.generationName, dataIndex: "name" },
                  {
                    title: t.buildStartedAt,
                    render: (_, row) => displayTime(row.created_at),
                  },
                  { title: t.generationCommit, dataIndex: "source_commit" },
                  {
                    title: t.generationCounts,
                    render: (_, row) => JSON.stringify(row.counts),
                  },
                  {
                    title: t.generationCurrent,
                    render: (_, row) =>
                      row.current ? (
                        t.generationCurrent
                      ) : (
                        <Button onClick={() => void switchGeneration(row.name)}>
                          {t.generationSwitch}
                        </Button>
                      ),
                  },
                ]}
              />
            </TableScroll>
          </section>
        </div>
        <div className="build-stack">
          {!selected && <div className="empty-state">{t.buildSelectHint}</div>}
          {selected && (
            <section className="card build-detail">
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
              <h3 className="card-title" style={{ marginTop: 16 }}>
                {t.buildStats}
              </h3>
              <TableScroll label={t.buildStats}>
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
                        countLabels[row.key]
                          ? t[countLabels[row.key]]
                          : row.key,
                    },
                    { title: "", dataIndex: "value" },
                  ]}
                />
              </TableScroll>
              <h3 className="card-title" style={{ marginTop: 16 }}>
                {t.buildLogs}
              </h3>
              <TableScroll label={t.buildLogs}>
                <Table
                  rowKey="index"
                  pagination={false}
                  dataSource={selected.logs.map((log, index) => ({
                    ...log,
                    index,
                  }))}
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
                            color:
                              row.level === "error" ? "#cf1322" : undefined,
                          }}
                        >
                          {row.message}
                        </span>
                      ),
                    },
                  ]}
                />
              </TableScroll>
            </section>
          )}
        </div>
      </div>
    </section>
  );
}
