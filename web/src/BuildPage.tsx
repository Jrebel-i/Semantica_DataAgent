import { PullsPanel } from "./PullsPanel";
import { ProjectSelector, storedProject } from "./ProjectSelector";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { TableScroll } from "./TableScroll";

type Build = components["schemas"]["BuildView"];
type Generations = components["schemas"]["GenerationsView"];
type ProjectSchedule = components["schemas"]["ProjectScheduleView"];
type BuildType = Build["build_type"];
type BuildStatus = Build["status"];

const TIMEZONE_PRESETS = [
  "Asia/Shanghai",
  "UTC",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Europe/London",
  "America/New_York",
];

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
  column_lineage: "entityColumnLineage",
  dimension_value: "entityDimensionValue",
  document: "entityDocument",
  relationship: "entityRelationship",
  chunk: "entityChunk",
  category: "entityCategory",
  embedding_inputs: "embeddingInputs",
  embedding_unique: "embeddingUnique",
  embedding_cache_hits: "embeddingCacheHits",
  embedding_cache_misses: "embeddingCacheMisses",
  stale_overrides: "staleOverridesCount",
};

const entitySummaryKeys = [
  "table",
  "field",
  "task",
  "lineage",
  "column_lineage",
  "dimension_value",
  "document",
  "relationship",
  "added",
  "changed",
  "deleted",
] as const;

function fallbackReasonLabel(
  reason: string | null | undefined,
  t: Messages,
): string | null {
  if (!reason) return null;
  switch (reason) {
    case "no_previous_full":
      return t.fallbackNoPreviousFull;
    case "config_changed":
      return t.fallbackConfigChanged;
    case "previous_full_failed":
      return t.fallbackPreviousFullFailed;
    case "threshold_exceeded":
      return t.fallbackThresholdExceeded;
    case "incremental_failed":
      return t.fallbackIncrementalFailed;
    default:
      return reason;
  }
}

function displayTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

function formatChangeSummary(build: Build, t: Messages): string {
  const parts = entitySummaryKeys
    .filter((key) => (build.counts[key] ?? 0) > 0)
    .slice(0, 3)
    .map((key) => `${t[countLabels[key]]} ${build.counts[key]}`);
  return parts.length ? parts.join(" · ") : "—";
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
  const [buildsLoaded, setBuildsLoaded] = useState(false);
  const [generations, setGenerations] = useState<Generations | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<Build | null>(null);
  const [typeFilter, setTypeFilter] = useState<"all" | BuildType>("all");
  const [detailTab, setDetailTab] = useState<"changes" | "logs">("changes");
  const [confirmFullOpen, setConfirmFullOpen] = useState(false);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleCron, setScheduleCron] = useState("");
  const [scheduleTimezone, setScheduleTimezone] = useState("Asia/Shanghai");
  const [scheduleNextRunAt, setScheduleNextRunAt] = useState<string | null>(
    null,
  );
  const [legacyIntervalSeconds, setLegacyIntervalSeconds] = useState(0);
  const [scheduleLegacy, setScheduleLegacy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const projectRef = useRef(project);
  projectRef.current = project;
  const mountedRef = useRef(true);
  const loadInFlightCount = useRef(0);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const projectQuery = `project=${encodeURIComponent(project)}`;
  const load = useCallback(async () => {
    const targetProject = project;
    loadInFlightCount.current += 1;
    try {
      const response = await fetch(`/api/v1/builds?${projectQuery}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const nextBuilds = (await response.json()) as Build[];
      if (!mountedRef.current || projectRef.current !== targetProject) return;
      setBuilds(Array.isArray(nextBuilds) ? nextBuilds : []);
      setBuildsLoaded(true);
      const versions = await fetch(`/api/v1/generations?${projectQuery}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (versions.ok) {
        const nextGenerations = (await versions.json()) as Generations;
        if (!mountedRef.current || projectRef.current !== targetProject) return;
        setGenerations(nextGenerations);
      }
    } finally {
      loadInFlightCount.current = Math.max(0, loadInFlightCount.current - 1);
    }
  }, [project, projectQuery, token]);
  useEffect(() => {
    setBuildsLoaded(false);
    setSelectedId(null);
    setSelectedDetail(null);
  }, [project]);
  useEffect(() => {
    let active = true;
    void fetch(`/api/v1/projects/${encodeURIComponent(project)}/schedule`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return;
        const saved = (await response.json()) as Partial<ProjectSchedule>;
        if (!active) return;
        setScheduleEnabled(Boolean(saved.enabled));
        setScheduleCron(saved.schedule_cron ?? "");
        setScheduleTimezone(saved.timezone || "Asia/Shanghai");
        setScheduleNextRunAt(saved.next_run_at ?? null);
        setLegacyIntervalSeconds(saved.interval_seconds ?? 0);
        setScheduleLegacy(Boolean(saved.legacy_interval));
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [project, token]);
  useEffect(() => {
    void load().catch(() => {
      if (mountedRef.current) {
        setBuildsLoaded(true);
        setError(t.buildError);
      }
    });
    const timer = window.setInterval(() => {
      if (loadInFlightCount.current > 0) return;
      void load().catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [load, t.buildError]);
  useEffect(() => {
    if (selectedId === null && builds.length) setSelectedId(builds[0].id);
  }, [builds, selectedId]);
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
  async function saveSchedule(nextEnabled = scheduleEnabled) {
    const trimmedCron = scheduleCron.trim();
    if (nextEnabled && !trimmedCron) {
      setNotice("");
      setError(t.scheduleCronRequired);
      return;
    }
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(project)}/schedule`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            enabled: nextEnabled,
            schedule_cron: trimmedCron || null,
            timezone: scheduleTimezone.trim() || "Asia/Shanghai",
          }),
        },
      );
      if (!response.ok) {
        let detail = t.buildError;
        try {
          const payload = (await response.json()) as { detail?: unknown };
          if (typeof payload.detail === "string" && payload.detail) {
            detail = payload.detail;
          }
        } catch {
          // Ignore non-JSON validation payloads
        }
        setNotice("");
        setError(detail);
        return;
      }
      const saved = (await response.json()) as Partial<ProjectSchedule>;
      setScheduleEnabled(Boolean(saved.enabled));
      setScheduleCron(saved.schedule_cron ?? "");
      setScheduleTimezone(saved.timezone || "Asia/Shanghai");
      setScheduleNextRunAt(saved.next_run_at ?? null);
      setLegacyIntervalSeconds(saved.interval_seconds ?? 0);
      setScheduleLegacy(Boolean(saved.legacy_interval));
      setNotice(t.scheduleSaved);
      setError("");
    } catch {
      setNotice("");
      setError(t.buildError);
    }
  }

  const latestBuild = builds[0] ?? null;
  const currentGen =
    generations?.generations?.find((gen) => gen.current) ?? null;
  const filteredBuilds =
    typeFilter === "all"
      ? builds
      : builds.filter((row) => row.build_type === typeFilter);
  const selected = builds.find((build) => build.id === selectedId) ?? null;
  const detail =
    selectedDetail && selected && selectedDetail.id === selected.id
      ? selectedDetail
      : selected;

  useEffect(() => {
    if (selectedId === null) {
      setSelectedDetail(null);
      return;
    }
    const controller = new AbortController();
    const targetProject = project;
    void fetch(`/api/v1/builds/${selectedId}?${projectQuery}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as Partial<Build> | null;
        if (
          controller.signal.aborted ||
          !mountedRef.current ||
          projectRef.current !== targetProject ||
          !data ||
          typeof data.id !== "number"
        ) {
          return;
        }
        setSelectedDetail(data as Build);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [
    project,
    projectQuery,
    selected?.finished_at,
    selected?.status,
    selectedId,
    token,
  ]);

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
      {generations?.behind && (
        <Alert
          type="warning"
          title={t.generationBehind}
          action={
            <Button onClick={() => setConfirmFullOpen(true)}>
              {t.buildConfirmAction}
            </Button>
          }
        />
      )}
      {notice && <Alert type="success" title={notice} />}
      {error && <Alert type="error" title={error} />}

      <section className="card section-card">
        <div className="card-title-row">
          <div>
            <h2 className="card-title">{t.buildOverviewTitle}</h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
              {t.buildOverviewHint}
            </p>
          </div>
          <div className="build-toolbar">
            <Button onClick={() => void trigger("incremental")}>
              {t.buildIncremental}
            </Button>
            <Button type="primary" onClick={() => setConfirmFullOpen(true)}>
              {t.buildFull}
            </Button>
          </div>
        </div>
        <div className="build-overview-grid">
          <div className="build-overview-stat">
            <span className="gov-metric-label">{t.buildMetricLatest}</span>
            <div className="build-overview-main">
              {buildsLoaded && latestBuild ? (
                <>
                  <Tag color={statusColors[latestBuild.status]}>
                    {t[latestBuild.status]}
                  </Tag>
                  <span>
                    {t[latestBuild.build_type]} #{latestBuild.id}
                  </span>
                </>
              ) : (
                <span>—</span>
              )}
            </div>
            <span className="muted" style={{ fontSize: 12.5 }}>
              {buildsLoaded && latestBuild
                ? `${displayTime(latestBuild.started_at)} · ${latestBuild.triggered_by ?? "—"}`
                : t.buildMetricOverviewChangesEmpty}
            </span>
          </div>
          <div className="build-overview-stat">
            <span className="gov-metric-label">
              {t.buildMetricOverviewChanges}
            </span>
            <div className="build-overview-main">
              <span>
                {buildsLoaded && latestBuild
                  ? formatChangeSummary(latestBuild, t)
                  : "—"}
              </span>
            </div>
            <span className="muted" style={{ fontSize: 12.5 }}>
              {buildsLoaded
                ? `${t.buildMetricQueueCount}: ${builds.length}`
                : "—"}
            </span>
          </div>
          <div className="build-overview-stat">
            <span className="gov-metric-label">{t.buildMetricGeneration}</span>
            <div className="build-overview-main">
              <span>{currentGen?.name ?? "—"}</span>
            </div>
            <span className="muted" style={{ fontSize: 12.5 }}>
              {currentGen?.source_commit
                ? `${currentGen.source_commit.slice(0, 7)} · ${
                    generations?.behind
                      ? t.buildSyncBehind
                      : t.buildSyncUpToDate
                  }`
                : generations?.behind
                  ? t.buildSyncBehind
                  : t.buildSyncUpToDate}
            </span>
          </div>
        </div>
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <div>
            <h2 className="card-title">{t.scheduleTitle}</h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
              {t.scheduleHint}
            </p>
          </div>
          <label className="source-auto-build">
            <span className="muted">
              {scheduleEnabled
                ? t.scheduleEnabledState
                : t.scheduleDisabledState}
            </span>
            <Switch
              aria-label={t.scheduleEnabled}
              checked={scheduleEnabled}
              onChange={(checked) => {
                setScheduleEnabled(checked);
                if (!checked || scheduleCron.trim()) {
                  void saveSchedule(checked);
                }
              }}
            />
          </label>
        </div>
        <div className="build-schedule-layout">
          <div>
            <div className="build-schedule-form">
              <div className="field">
                <label className="field-label" htmlFor="build-schedule-cron">
                  {t.scheduleCron}
                </label>
                <Input
                  id="build-schedule-cron"
                  aria-label={t.scheduleCron}
                  placeholder={t.scheduleCronPlaceholder}
                  value={scheduleCron}
                  onChange={(event) => setScheduleCron(event.target.value)}
                />
              </div>
              <label className="field">
                <span className="field-label">{t.scheduleTimezone}</span>
                <select
                  id="build-schedule-timezone"
                  aria-label={t.scheduleTimezone}
                  value={scheduleTimezone}
                  onChange={(event) => setScheduleTimezone(event.target.value)}
                >
                  {(TIMEZONE_PRESETS.includes(scheduleTimezone)
                    ? TIMEZONE_PRESETS
                    : [scheduleTimezone, ...TIMEZONE_PRESETS]
                  ).map((tz) => (
                    <option key={tz} value={tz}>
                      {tz}
                    </option>
                  ))}
                </select>
              </label>
              <Button type="primary" onClick={() => void saveSchedule()}>
                {t.scheduleSave}
              </Button>
            </div>
            <p className="field-hint" style={{ marginTop: 10 }}>
              {scheduleLegacy
                ? t.scheduleLegacyIntervalHint.replace(
                    "{seconds}",
                    String(legacyIntervalSeconds),
                  )
                : scheduleEnabled && scheduleNextRunAt
                  ? `${t.scheduleCronHint} · ${t.scheduleNextRun}: ${displayTime(scheduleNextRunAt)}`
                  : scheduleEnabled
                    ? t.scheduleCronHint
                    : t.scheduleDisabledHint}
            </p>
          </div>
          <div className="gov-info-banner gov-info-banner--stack">
            <div>{t.buildScheduleAutoNoteUpload}</div>
            <div>{t.buildScheduleAutoNoteCorrection}</div>
          </div>
        </div>
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.builds}</h2>
          <Select
            aria-label={t.buildType}
            value={typeFilter}
            style={{ minWidth: 140 }}
            onChange={(value) => setTypeFilter(value)}
            options={[
              { value: "all", label: t.buildAllTypes },
              { value: "incremental", label: t.incremental },
              { value: "full", label: t.full },
            ]}
          />
        </div>
        <TableScroll label={t.builds}>
          <Table
            rowKey="id"
            dataSource={filteredBuilds}
            locale={{ emptyText: t.noBuilds }}
            pagination={false}
            rowClassName={(row) =>
              row.id === selectedId ? "build-table-row-selected" : ""
            }
            onRow={(row) => ({ onClick: () => setSelectedId(row.id) })}
            columns={[
              { title: t.buildId, dataIndex: "id" },
              {
                title: t.buildStatus,
                render: (_, row) => {
                  const fallbackLabel = fallbackReasonLabel(
                    row.fallback_reason,
                    t,
                  );
                  return (
                    <div className="build-status-tags">
                      <Tag color={statusColors[row.status]}>
                        {t[row.status]}
                      </Tag>
                      {fallbackLabel && (
                        <Tag color="orange">
                          {t.fallbackBadge.replace("{reason}", fallbackLabel)}
                        </Tag>
                      )}
                      {(row.stale_overrides?.length ?? 0) > 0 && (
                        <Tag color="warning">
                          {t.staleOverridesBadge.replace(
                            "{count}",
                            String(row.stale_overrides?.length ?? 0),
                          )}
                        </Tag>
                      )}
                    </div>
                  );
                },
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
                title: t.buildChangeSummaryCol,
                render: (_, row) => formatChangeSummary(row, t),
              },
              {
                title: t.buildFinishedAt,
                render: (_, row) => displayTime(row.finished_at),
              },
            ]}
          />
        </TableScroll>
      </section>

      {!detail ? (
        <div className="empty-state">{t.buildSelectHint}</div>
      ) : (
        <section className="card section-card build-detail">
          <div className="card-title-row">
            <div>
              <h2 className="card-title">{`${t.builds} #${detail.id}`}</h2>
              <p
                className="muted"
                style={{ margin: "2px 0 0", fontSize: 12.5 }}
              >
                {`${t[detail.build_type]} · ${displayTime(detail.started_at)} · ${detail.triggered_by ?? "—"}`}
              </p>
            </div>
            <Tag color={statusColors[detail.status]}>{t[detail.status]}</Tag>
          </div>
          <div className="source-view-tabs">
            <button
              type="button"
              className={detailTab === "changes" ? "active" : ""}
              onClick={() => setDetailTab("changes")}
            >
              {t.buildTabChanges}
            </button>
            <button
              type="button"
              className={detailTab === "logs" ? "active" : ""}
              onClick={() => setDetailTab("logs")}
            >
              {t.buildTabLogs}
            </button>
          </div>

          {detailTab === "changes" ? (
            <div className="stack">
              {detail.fallback_reason && (
                <Alert
                  type="warning"
                  showIcon
                  title={t.fallbackNoticeTitle}
                  description={fallbackReasonLabel(detail.fallback_reason, t)}
                />
              )}
              {(detail.stale_overrides?.length ?? 0) > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  title={t.staleOverridesTitle}
                  description={
                    <ul className="stale-overrides-list">
                      {(detail.stale_overrides ?? []).map((item, idx) => (
                        <li key={`${item.kind}-${item.key}-${idx}`}>
                          <strong>
                            [{item.kind}] {item.key}
                          </strong>
                          ：{item.reason}
                        </li>
                      ))}
                    </ul>
                  }
                />
              )}
              <div className="build-detail-split">
                <div className="build-detail-box">
                  <h3>{t.buildSummarySection}</h3>
                  {entitySummaryKeys.some(
                    (key) => (detail.counts[key] ?? 0) > 0,
                  ) && (
                    <Space wrap style={{ marginBottom: 12 }}>
                      {entitySummaryKeys
                        .filter((key) => (detail.counts[key] ?? 0) > 0)
                        .map((key) => (
                          <Tag key={key} color="blue">
                            {`${t[countLabels[key]]}: ${detail.counts[key]}`}
                          </Tag>
                        ))}
                    </Space>
                  )}
                  <TableScroll label={t.buildStats}>
                    <Table
                      rowKey="key"
                      pagination={false}
                      size="small"
                      dataSource={Object.entries(detail.counts)
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
                </div>
                <div className="build-detail-box">
                  <h3>{t.buildInfoSection}</h3>
                  <Descriptions
                    column={1}
                    size="small"
                    items={[
                      {
                        key: "status",
                        label: t.buildStatus,
                        children: t[detail.status],
                      },
                      {
                        key: "type",
                        label: t.buildType,
                        children: t[detail.build_type],
                      },
                      {
                        key: "triggered",
                        label: t.buildTriggeredBy,
                        children: detail.triggered_by ?? "—",
                      },
                      {
                        key: "commit",
                        label: t.buildSourceCommit,
                        children: detail.source_commit ?? "—",
                      },
                      {
                        key: "started",
                        label: t.buildStartedAt,
                        children: displayTime(detail.started_at),
                      },
                      {
                        key: "finished",
                        label: t.buildFinishedAt,
                        children: displayTime(detail.finished_at),
                      },
                      ...(detail.error
                        ? [
                            {
                              key: "error",
                              label: t.failed,
                              children: detail.error,
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              </div>
            </div>
          ) : (
            <TableScroll label={t.buildLogs}>
              <Table
                rowKey="index"
                pagination={false}
                dataSource={detail.logs.map((log, index) => ({
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
                        className={
                          row.level === "error" ? "build-log-error" : undefined
                        }
                      >
                        {row.message}
                      </span>
                    ),
                  },
                ]}
              />
            </TableScroll>
          )}
        </section>
      )}

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

      <section className="card section-card">
        <details className="build-pulls-details">
          <summary>
            <span>{t.buildPullCollapseTitle}</span>
          </summary>
          <p className="muted" style={{ margin: "6px 0 0", fontSize: 12.5 }}>
            {t.buildPullCollapseHint}
          </p>
          <div className="build-pulls-body">
            <PullsPanel token={token} project={project} messages={t} />
          </div>
        </details>
      </section>

      <Modal
        open={confirmFullOpen}
        title={t.buildConfirmFullTitle}
        onCancel={() => setConfirmFullOpen(false)}
        okText={t.buildConfirmAction}
        cancelText={t.buildConfirmCancel}
        onOk={() => {
          setConfirmFullOpen(false);
          void trigger("full");
        }}
      >
        <p style={{ margin: "8px 0" }}>{t.buildConfirmFullDesc}</p>
      </Modal>
    </section>
  );
}
