import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Space, Tag } from "antd";
import type { components } from "./api";
import { readErrorDetail } from "./httpErrors";
import type { Messages } from "./i18n";
import type { Project } from "./ProjectSelector";

type DiscoveryConfig = components["schemas"]["DiscoveryConfigView"];
type DiscoveryConfigUpdate = components["schemas"]["DiscoveryConfigUpdate"];
type DiscoveryPresetTemplate = components["schemas"]["DiscoveryPresetTemplate"];
type DiscoveryRun = components["schemas"]["DiscoveryRunView"];
type DiscoveryRunDetail = components["schemas"]["DiscoveryRunDetailView"];
type DiscoveryCandidate = components["schemas"]["DiscoveryCandidateView"];
type DiscoveryRunTrigger = components["schemas"]["DiscoveryRunTrigger"];
type Provider = components["schemas"]["ProviderView"];
type DiscoveryPreset = DiscoveryConfig["preset"];

export const FALLBACK_DISCOVERY_PRESETS: Record<
  DiscoveryPreset,
  DiscoveryPresetTemplate
> = {
  doris_audit_log: {
    preset: "doris_audit_log",
    source_table: "__internal_schema.audit_log",
    sql_column: "stmt",
    status_column: "state",
    time_column: "time",
    database_column: "db",
    caller_column: "user",
    dialect_column: null,
    success_statuses: ["EOF", "OK"],
  },
  dtb_tracking_table: {
    preset: "dtb_tracking_table",
    source_table: "mysql.data_tool_box_emr.query_history",
    sql_column: "query_sql",
    status_column: "status",
    time_column: "create_time",
    database_column: "database_name",
    caller_column: "user_name",
    dialect_column: "engine_type",
    success_statuses: ["SUCCESS", "SUCCEEDED", "FINISHED"],
  },
};

const DEFAULT_DISCOVERY_CONFIG: DiscoveryConfig = {
  ...FALLBACK_DISCOVERY_PRESETS.doris_audit_log,
  lookback_days: 7,
  max_queries: 5000,
  min_frequency: 3,
  max_fk_candidates: 20,
  sample_limit: 1000,
  min_inclusion_ratio: 0.8,
  probe_timeout_seconds: 30,
  probe_concurrency: 2,
  provider_id: null,
  model: null,
  presets: FALLBACK_DISCOVERY_PRESETS,
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function formatOverlapRatio(
  ratio: number | null | undefined,
): string | null {
  if (typeof ratio !== "number" || !Number.isFinite(ratio)) return null;
  return `${(ratio * 100).toFixed(1)}%`;
}

export function formatProbePartitions(
  partitions: Record<string, Record<string, string>> | null | undefined,
): string {
  if (!partitions || typeof partitions !== "object") return "";
  const parts: string[] = [];
  for (const [table, cols] of Object.entries(partitions)) {
    if (!cols || typeof cols !== "object") continue;
    const pairs = Object.entries(cols)
      .filter(([col, val]) => Boolean(col) && val !== undefined && val !== null)
      .map(([col, val]) => `${col}=${String(val)}`);
    if (pairs.length > 0) {
      parts.push(`${table}(${pairs.join(", ")})`);
    }
  }
  return parts.join(" · ");
}

function parseStatusList(raw: string): string[] {
  const seen = new Set<string>();
  const items: string[] = [];
  for (const piece of raw.split(/[,，\s]+/)) {
    const trimmed = piece.trim();
    if (trimmed && !seen.has(trimmed)) {
      seen.add(trimmed);
      items.push(trimmed);
    }
  }
  return items;
}

function parseOptionalInt(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseOptionalFloat(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseFloat(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function runStatusColor(status: DiscoveryRun["status"]): string {
  if (status === "completed") return "green";
  if (status === "running") return "blue";
  if (status === "failed") return "red";
  return "orange";
}

function runStatusLabel(status: DiscoveryRun["status"], t: Messages): string {
  if (status === "completed") return t.discoveryStatusCompleted;
  if (status === "running") return t.discoveryStatusRunning;
  if (status === "partial") return t.discoveryStatusPartial;
  if (status === "failed") return t.discoveryStatusFailed;
  return t.discoveryStatusIncomplete;
}

function candidateKindLabel(
  kind: DiscoveryCandidate["candidate_kind"],
  t: Messages,
): string {
  if (kind === "relationship") return t.discoveryKindRelationship;
  if (kind === "metric") return t.discoveryKindMetric;
  if (kind === "filter_rule") return t.discoveryKindFilterRule;
  return t.discoveryKindEvalDraft;
}

function candidateDecisionMeta(
  decision: DiscoveryCandidate["decision"],
  t: Messages,
): { label: string; color: string } {
  if (decision === "proposed")
    return { label: t.discoveryDecisionProposed, color: "green" };
  if (decision === "skipped")
    return { label: t.discoveryDecisionSkipped, color: "orange" };
  if (decision === "failed")
    return { label: t.discoveryDecisionFailed, color: "red" };
  if (decision === "rejected")
    return { label: t.discoveryDecisionRejected, color: "red" };
  return { label: t.discoveryDecisionDeduplicated, color: "default" };
}

function CandidateCard({
  candidate,
  t,
  onNavigateCorrections,
}: {
  candidate: DiscoveryCandidate;
  t: Messages;
  onNavigateCorrections?: (proposalId?: string) => void;
}) {
  const decisionMeta = candidateDecisionMeta(candidate.decision, t);
  const ev = (candidate.evidence ?? {}) as Record<string, unknown>;
  const freq =
    typeof ev.query_history_frequency === "number"
      ? ev.query_history_frequency
      : null;
  const callers =
    typeof ev.distinct_callers === "number" ? ev.distinct_callers : null;
  const sampleCount =
    typeof ev.sample_distinct_count === "number"
      ? ev.sample_distinct_count
      : null;
  const matchedCount =
    typeof ev.matched_count === "number" ? ev.matched_count : null;
  const overlapText = formatOverlapRatio(
    typeof ev.overlap_ratio === "number" ? ev.overlap_ratio : null,
  );
  const joinMatched =
    typeof ev.join_matched === "boolean" ? ev.join_matched : null;
  const probingStatus =
    typeof ev.probing_status === "string" && ev.probing_status
      ? ev.probing_status
      : null;
  const partitionsText = formatProbePartitions(
    ev.probe_partitions as Record<string, Record<string, string>> | undefined,
  );

  return (
    <article className="discovery-candidate-card">
      <div className="evolution-round-header">
        <Space size={6} wrap>
          <Tag>{candidateKindLabel(candidate.candidate_kind, t)}</Tag>
          <strong className="mono">{candidate.target}</strong>
        </Space>
        <Space size={6} wrap>
          <Tag color={decisionMeta.color}>{decisionMeta.label}</Tag>
          {candidate.proposal_id && (
            <a
              className="discovery-proposal-link"
              href={`/corrections?proposal_id=${encodeURIComponent(candidate.proposal_id)}`}
              onClick={(event) => {
                if (onNavigateCorrections) {
                  event.preventDefault();
                  onNavigateCorrections(candidate.proposal_id ?? undefined);
                }
              }}
            >
              {t.discoveryViewProposal}
            </a>
          )}
          {candidate.eval_question_id && (
            <Tag className="mono">
              {t.discoveryEvalDraftId}: {candidate.eval_question_id}
            </Tag>
          )}
        </Space>
      </div>
      <div className="evolution-round-body">
        {candidate.reason && (
          <div className="muted">
            {t.discoveryReasonLabel}: {candidate.reason}
          </div>
        )}
        <div className="discovery-evidence-pills">
          {freq !== null && (
            <Tag>
              {t.discoveryEvidenceFrequency}: {freq}
            </Tag>
          )}
          {callers !== null && (
            <Tag>
              {t.discoveryEvidenceDistinctCallers}: {callers}
            </Tag>
          )}
          {sampleCount !== null && (
            <Tag>
              {t.discoveryEvidenceSampleCount}: {sampleCount}
            </Tag>
          )}
          {matchedCount !== null && (
            <Tag>
              {t.discoveryEvidenceMatchedCount}: {matchedCount}
            </Tag>
          )}
          {overlapText && (
            <Tag color="blue">
              {t.discoveryEvidenceOverlapRatio}: {overlapText}
            </Tag>
          )}
          {joinMatched !== null && (
            <Tag color={joinMatched ? "green" : "red"}>
              {t.discoveryEvidenceJoinProbe}:{" "}
              {joinMatched
                ? t.discoveryEvidenceJoinPassed
                : t.discoveryEvidenceJoinFailed}
            </Tag>
          )}
          {probingStatus && probingStatus !== "not_applicable" && (
            <Tag className="mono">
              {t.discoveryEvidenceProbingStatus}: {probingStatus}
            </Tag>
          )}
        </div>
        {partitionsText && (
          <div className="muted mono">
            {t.discoveryEvidencePartitions}: {partitionsText}
          </div>
        )}
      </div>
    </article>
  );
}

export function DiscoveryPanel({
  token,
  project,
  projectInfo,
  providers,
  messages: t,
  onNavigateCorrections,
  onNavigatePersonalSettings,
}: {
  token: string;
  project: string;
  projectInfo: Project | null;
  providers: Provider[];
  messages: Messages;
  onNavigateCorrections?: (proposalId?: string) => void;
  onNavigatePersonalSettings?: () => void;
}) {
  const projectRef = useRef(project);
  projectRef.current = project;
  const requestGenRef = useRef(0);
  const detailGenRef = useRef(0);
  const pollInFlightRef = useRef(false);

  const isDocumentProject = projectInfo?.type === "document";
  const isArchivedProject =
    projectInfo?.status === "archived" || projectInfo?.status === "deleting";

  const [dtbConfigured, setDtbConfigured] = useState<boolean | null>(null);
  const [config, setConfig] = useState<DiscoveryConfig>(
    DEFAULT_DISCOVERY_CONFIG,
  );
  const [successStatusesText, setSuccessStatusesText] = useState(
    DEFAULT_DISCOVERY_CONFIG.success_statuses.join(", "),
  );
  const [runs, setRuns] = useState<DiscoveryRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedDetail, setSelectedDetail] =
    useState<DiscoveryRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Trigger panel state: seed_eval_drafts MUST default to false
  const [mineQueryHistory, setMineQueryHistory] = useState(true);
  const [probeRelationships, setProbeRelationships] = useState(true);
  const [seedEvalDrafts, setSeedEvalDrafts] = useState(false);
  const [overrideLookbackDays, setOverrideLookbackDays] = useState("");
  const [overrideMaxQueries, setOverrideMaxQueries] = useState("");
  const [overrideMaxFkCandidates, setOverrideMaxFkCandidates] = useState("");
  const [overrideSampleLimit, setOverrideSampleLimit] = useState("");
  const [overrideMinInclusionRatio, setOverrideMinInclusionRatio] =
    useState("");
  const [overrideProbeTimeout, setOverrideProbeTimeout] = useState("");

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [unsupportedByServer, setUnsupportedByServer] = useState(false);
  const [error, setError] = useState("");
  const [saveNotice, setSaveNotice] = useState("");

  const loadDtbTokenStatus = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const response = await fetch("/api/v1/me/dtb-token", {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        });
        if (!response.ok) return;
        const data = (await response.json()) as { configured?: boolean };
        if (!signal?.aborted) {
          setDtbConfigured(Boolean(data.configured));
        }
      } catch {
        // Ignore abort / transient failures
      }
    },
    [token],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadDtbTokenStatus(controller.signal);
    return () => controller.abort();
  }, [loadDtbTokenStatus]);

  const loadProjectData = useCallback(
    async (targetProject: string, signal?: AbortSignal) => {
      const gen = requestGenRef.current;
      const headers = { Authorization: `Bearer ${token}` };
      const encoded = encodeURIComponent(targetProject);
      const [cfgRes, runsRes] = await Promise.all([
        fetch(`/api/v1/projects/${encoded}/discovery-config`, {
          headers,
          signal,
        }),
        fetch(`/api/v1/discovery/runs?project=${encoded}`, {
          headers,
          signal,
        }),
      ]);
      if (cfgRes.status === 422) {
        if (
          !signal?.aborted &&
          requestGenRef.current === gen &&
          projectRef.current === targetProject
        ) {
          setUnsupportedByServer(true);
        }
        return;
      }
      if (!cfgRes.ok) {
        throw new Error(await readErrorDetail(cfgRes, t.error));
      }
      if (!runsRes.ok) {
        throw new Error(await readErrorDetail(runsRes, t.error));
      }
      const nextConfig = (await cfgRes.json()) as DiscoveryConfig;
      const nextRuns = (await runsRes.json()) as DiscoveryRun[];
      if (
        signal?.aborted ||
        requestGenRef.current !== gen ||
        projectRef.current !== targetProject
      ) {
        return;
      }
      setUnsupportedByServer(false);
      setConfig({
        ...DEFAULT_DISCOVERY_CONFIG,
        ...nextConfig,
        presets: nextConfig.presets ?? FALLBACK_DISCOVERY_PRESETS,
      });
      setSuccessStatusesText((nextConfig.success_statuses ?? []).join(", "));
      const safeRuns = Array.isArray(nextRuns) ? nextRuns : [];
      setRuns(safeRuns);
      setSelectedRunId((prev) =>
        prev && safeRuns.some((r) => r.id === prev)
          ? prev
          : (safeRuns[0]?.id ?? ""),
      );
    },
    [token, t.error],
  );

  useEffect(() => {
    const controller = new AbortController();
    requestGenRef.current += 1;
    detailGenRef.current += 1;
    setError("");
    setSaveNotice("");
    setUnsupportedByServer(false);
    setRuns([]);
    setSelectedRunId("");
    setSelectedDetail(null);
    setConfig(DEFAULT_DISCOVERY_CONFIG);
    setSuccessStatusesText(
      DEFAULT_DISCOVERY_CONFIG.success_statuses.join(", "),
    );

    if (isDocumentProject) {
      setLoading(false);
      return () => controller.abort();
    }

    setLoading(true);
    void loadProjectData(project, controller.signal)
      .catch((failure) => {
        if (!controller.signal.aborted && projectRef.current === project) {
          setError(failure instanceof Error ? failure.message : t.error);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [isDocumentProject, loadProjectData, project, t.error]);

  const loadRunDetail = useCallback(
    async (targetProject: string, runId: string, signal?: AbortSignal) => {
      const detailGen = detailGenRef.current;
      const reqGen = requestGenRef.current;
      const response = await fetch(
        `/api/v1/discovery/runs/${encodeURIComponent(runId)}?project=${encodeURIComponent(targetProject)}`,
        {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const detail = (await response.json()) as DiscoveryRunDetail;
      if (
        signal?.aborted ||
        detailGenRef.current !== detailGen ||
        requestGenRef.current !== reqGen ||
        projectRef.current !== targetProject
      ) {
        return;
      }
      setSelectedDetail(detail);
    },
    [token, t.error],
  );

  useEffect(() => {
    if (!selectedRunId || isDocumentProject || unsupportedByServer) {
      setSelectedDetail(null);
      return;
    }
    const controller = new AbortController();
    detailGenRef.current += 1;
    setDetailLoading(true);
    void loadRunDetail(project, selectedRunId, controller.signal)
      .catch((failure) => {
        if (!controller.signal.aborted && projectRef.current === project) {
          setError(failure instanceof Error ? failure.message : t.error);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setDetailLoading(false);
        }
      });
    return () => controller.abort();
  }, [
    isDocumentProject,
    loadRunDetail,
    project,
    selectedRunId,
    unsupportedByServer,
  ]);

  const hasRunning = runs.some((r) => r.status === "running");

  useEffect(() => {
    if (!hasRunning || isDocumentProject || unsupportedByServer) return;
    const timer = window.setInterval(() => {
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      const currentProject = projectRef.current;
      const tasks: Promise<unknown>[] = [
        loadProjectData(currentProject).catch(() => {
          // Keep last valid state on transient poll error
        }),
      ];
      if (selectedRunId) {
        tasks.push(
          loadRunDetail(currentProject, selectedRunId).catch(() => {
            // Keep last valid state on transient poll error
          }),
        );
      }
      void Promise.allSettled(tasks).finally(() => {
        pollInFlightRef.current = false;
      });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [
    hasRunning,
    isDocumentProject,
    loadProjectData,
    loadRunDetail,
    selectedRunId,
    unsupportedByServer,
  ]);

  function handlePresetChange(nextPreset: DiscoveryPreset) {
    const presetMap = config.presets ?? FALLBACK_DISCOVERY_PRESETS;
    const template =
      presetMap[nextPreset] ?? FALLBACK_DISCOVERY_PRESETS[nextPreset];
    setConfig((prev) => ({
      ...prev,
      preset: nextPreset,
      source_table: template.source_table,
      sql_column: template.sql_column,
      status_column: template.status_column,
      time_column: template.time_column,
      database_column: template.database_column ?? null,
      caller_column: template.caller_column ?? null,
      dialect_column: template.dialect_column ?? null,
      success_statuses: [...template.success_statuses],
    }));
    setSuccessStatusesText(template.success_statuses.join(", "));
  }

  async function saveConfig() {
    if (isArchivedProject || isDocumentProject) return;
    const targetProject = project;
    const parsedStatuses = parseStatusList(successStatusesText);
    setSaving(true);
    setError("");
    setSaveNotice("");
    const payload: DiscoveryConfigUpdate = {
      preset: config.preset,
      source_table: config.source_table.trim(),
      sql_column: config.sql_column.trim(),
      status_column: config.status_column.trim(),
      time_column: config.time_column.trim(),
      database_column: config.database_column?.trim() || null,
      caller_column: config.caller_column?.trim() || null,
      dialect_column: config.dialect_column?.trim() || null,
      success_statuses: parsedStatuses,
      lookback_days: config.lookback_days,
      max_queries: config.max_queries,
      min_frequency: config.min_frequency,
      max_fk_candidates: config.max_fk_candidates,
      sample_limit: config.sample_limit,
      min_inclusion_ratio: config.min_inclusion_ratio,
      probe_timeout_seconds: config.probe_timeout_seconds,
      probe_concurrency: config.probe_concurrency,
      provider_id: config.provider_id ?? null,
      model: config.model ?? null,
    };
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(targetProject)}/discovery-config`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const updated = (await response.json()) as DiscoveryConfig;
      if (projectRef.current === targetProject) {
        setConfig({
          ...DEFAULT_DISCOVERY_CONFIG,
          ...updated,
          presets: updated.presets ?? FALLBACK_DISCOVERY_PRESETS,
        });
        setSuccessStatusesText((updated.success_statuses ?? []).join(", "));
        setSaveNotice(t.discoveryConfigSaved);
      }
    } catch (failure) {
      if (projectRef.current === targetProject) {
        setError(failure instanceof Error ? failure.message : t.error);
      }
    } finally {
      setSaving(false);
    }
  }

  async function triggerRun() {
    if (isArchivedProject || isDocumentProject || dtbConfigured !== true)
      return;
    const targetProject = project;
    setTriggering(true);
    setError("");
    const payload: DiscoveryRunTrigger = {
      mine_query_history: mineQueryHistory,
      probe_relationships: probeRelationships,
      seed_eval_drafts: seedEvalDrafts,
    };
    const lb = parseOptionalInt(overrideLookbackDays);
    if (lb !== undefined) payload.lookback_days = lb;
    const mq = parseOptionalInt(overrideMaxQueries);
    if (mq !== undefined) payload.max_queries = mq;
    const mfk = parseOptionalInt(overrideMaxFkCandidates);
    if (mfk !== undefined) payload.max_fk_candidates = mfk;
    const sl = parseOptionalInt(overrideSampleLimit);
    if (sl !== undefined) payload.sample_limit = sl;
    const mir = parseOptionalFloat(overrideMinInclusionRatio);
    if (mir !== undefined) payload.min_inclusion_ratio = mir;
    const pt = parseOptionalInt(overrideProbeTimeout);
    if (pt !== undefined) payload.probe_timeout_seconds = pt;

    try {
      const response = await fetch(
        `/api/v1/discovery/runs?project=${encodeURIComponent(targetProject)}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const created = (await response.json()) as DiscoveryRun;
      if (projectRef.current === targetProject) {
        setRuns((prev) => [
          created,
          ...prev.filter((r) => r.id !== created.id),
        ]);
        setSelectedRunId(created.id);
      }
    } catch (failure) {
      if (projectRef.current === targetProject) {
        setError(failure instanceof Error ? failure.message : t.error);
      }
    } finally {
      setTriggering(false);
    }
  }

  const selectedProvider = providers.find((p) => p.id === config.provider_id);
  const availableModels = selectedProvider?.models ?? [];
  const modelOptions =
    config.model && !availableModels.includes(config.model)
      ? [config.model, ...availableModels]
      : availableModels;

  const selectedRunSummary = useMemo(
    () => runs.find((r) => r.id === selectedRunId) ?? runs[0] ?? null,
    [runs, selectedRunId],
  );
  const activeRun =
    selectedDetail && selectedDetail.id === selectedRunSummary?.id
      ? selectedDetail
      : selectedRunSummary;
  const activeCandidates =
    selectedDetail && selectedDetail.id === activeRun?.id
      ? (selectedDetail.candidates ?? [])
      : [];

  const noScopeSelected =
    !mineQueryHistory && !probeRelationships && !seedEvalDrafts;
  const triggerDisabled =
    isArchivedProject ||
    dtbConfigured !== true ||
    hasRunning ||
    noScopeSelected;

  if (isDocumentProject || unsupportedByServer) {
    return (
      <div className="stack">
        <Alert type="warning" showIcon title={t.discoveryDocumentUnsupported} />
      </div>
    );
  }

  return (
    <div className="stack">
      {isArchivedProject && (
        <Alert type="warning" showIcon title={t.discoveryArchivedReadonly} />
      )}
      {dtbConfigured === false && (
        <Alert
          type="warning"
          showIcon
          title={t.discoveryDtbMissingTitle}
          description={
            <div className="discovery-alert-body">
              <span>{t.discoveryDtbMissingDesc}</span>
              <a
                href="/personal-settings"
                onClick={(event) => {
                  if (onNavigatePersonalSettings) {
                    event.preventDefault();
                    onNavigatePersonalSettings();
                  }
                }}
              >
                {t.discoveryGoPersonalSettings}
              </a>
            </div>
          }
        />
      )}
      {error && <Alert type="error" title={error} showIcon />}
      {saveNotice && <Alert type="success" title={saveNotice} showIcon />}

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.discoveryConfigTitle}</h2>
          <Space size={8} wrap>
            {dtbConfigured === true && (
              <Tag color="green">{t.discoveryDtbReady}</Tag>
            )}
            <Button
              size="small"
              loading={loading}
              onClick={() => {
                void loadDtbTokenStatus();
                void loadProjectData(projectRef.current).catch((failure) => {
                  setError(
                    failure instanceof Error ? failure.message : t.error,
                  );
                });
              }}
            >
              {t.refresh}
            </Button>
          </Space>
        </div>

        <div className="evolution-config-grid">
          <div className="evolution-field">
            <label htmlFor="disc-preset">{t.discoveryPresetLabel}</label>
            <select
              id="disc-preset"
              value={config.preset}
              disabled={isArchivedProject}
              onChange={(e) =>
                handlePresetChange(e.target.value as DiscoveryPreset)
              }
            >
              <option value="doris_audit_log">{t.discoveryPresetDoris}</option>
              <option value="dtb_tracking_table">{t.discoveryPresetDtb}</option>
            </select>
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-source-table">{t.discoverySourceTable}</label>
            <input
              id="disc-source-table"
              type="text"
              disabled={isArchivedProject}
              value={config.source_table}
              onChange={(e) =>
                setConfig((prev) => ({ ...prev, source_table: e.target.value }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-sql-col">{t.discoverySqlColumn}</label>
            <input
              id="disc-sql-col"
              type="text"
              disabled={isArchivedProject}
              value={config.sql_column}
              onChange={(e) =>
                setConfig((prev) => ({ ...prev, sql_column: e.target.value }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-status-col">{t.discoveryStatusColumn}</label>
            <input
              id="disc-status-col"
              type="text"
              disabled={isArchivedProject}
              value={config.status_column}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  status_column: e.target.value,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-time-col">{t.discoveryTimeColumn}</label>
            <input
              id="disc-time-col"
              type="text"
              disabled={isArchivedProject}
              value={config.time_column}
              onChange={(e) =>
                setConfig((prev) => ({ ...prev, time_column: e.target.value }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-db-col">{t.discoveryDatabaseColumn}</label>
            <input
              id="disc-db-col"
              type="text"
              disabled={isArchivedProject}
              value={config.database_column ?? ""}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  database_column: e.target.value || null,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-caller-col">{t.discoveryCallerColumn}</label>
            <input
              id="disc-caller-col"
              type="text"
              disabled={isArchivedProject}
              value={config.caller_column ?? ""}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  caller_column: e.target.value || null,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-dialect-col">{t.discoveryDialectColumn}</label>
            <input
              id="disc-dialect-col"
              type="text"
              disabled={isArchivedProject}
              value={config.dialect_column ?? ""}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  dialect_column: e.target.value || null,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-success-statuses">
              {t.discoverySuccessStatuses}
            </label>
            <input
              id="disc-success-statuses"
              type="text"
              disabled={isArchivedProject}
              value={successStatusesText}
              onChange={(e) => setSuccessStatusesText(e.target.value)}
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-lookback-days">
              {t.discoveryLookbackDays}
            </label>
            <input
              id="disc-lookback-days"
              type="number"
              min={1}
              max={90}
              disabled={isArchivedProject}
              value={config.lookback_days}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  lookback_days: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-max-queries">{t.discoveryMaxQueries}</label>
            <input
              id="disc-max-queries"
              type="number"
              min={50}
              max={20000}
              disabled={isArchivedProject}
              value={config.max_queries}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  max_queries: Number(e.target.value) || 50,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-min-frequency">
              {t.discoveryMinFrequency}
            </label>
            <input
              id="disc-min-frequency"
              type="number"
              min={1}
              max={500}
              disabled={isArchivedProject}
              value={config.min_frequency}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  min_frequency: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-max-fk">{t.discoveryMaxFkCandidates}</label>
            <input
              id="disc-max-fk"
              type="number"
              min={0}
              max={50}
              disabled={isArchivedProject}
              value={config.max_fk_candidates}
              onChange={(e) => {
                const next = Number(e.target.value);
                setConfig((prev) => ({
                  ...prev,
                  max_fk_candidates: Number.isFinite(next)
                    ? Math.max(0, next)
                    : 0,
                }));
              }}
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-sample-limit">{t.discoverySampleLimit}</label>
            <input
              id="disc-sample-limit"
              type="number"
              min={100}
              max={5000}
              disabled={isArchivedProject}
              value={config.sample_limit}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  sample_limit: Number(e.target.value) || 100,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-min-ratio">
              {t.discoveryMinInclusionRatio}
            </label>
            <input
              id="disc-min-ratio"
              type="number"
              min={0.5}
              max={1.0}
              step={0.05}
              disabled={isArchivedProject}
              value={config.min_inclusion_ratio}
              onChange={(e) => {
                const next = Number(e.target.value);
                setConfig((prev) => ({
                  ...prev,
                  min_inclusion_ratio: Number.isFinite(next) ? next : 0.8,
                }));
              }}
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-probe-timeout">
              {t.discoveryProbeTimeoutSeconds}
            </label>
            <input
              id="disc-probe-timeout"
              type="number"
              min={5}
              max={120}
              disabled={isArchivedProject}
              value={config.probe_timeout_seconds}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  probe_timeout_seconds: Number(e.target.value) || 5,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-probe-concurrency">
              {t.discoveryProbeConcurrency}
            </label>
            <input
              id="disc-probe-concurrency"
              type="number"
              min={1}
              max={2}
              disabled={isArchivedProject}
              value={config.probe_concurrency}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  probe_concurrency: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-provider">{t.discoveryProviderLabel}</label>
            <select
              id="disc-provider"
              disabled={isArchivedProject}
              value={config.provider_id ?? ""}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  provider_id: e.target.value || null,
                  model: null,
                }))
              }
            >
              <option value="">{t.evolutionModelDefault}</option>
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                </option>
              ))}
            </select>
          </div>

          <div className="evolution-field">
            <label htmlFor="disc-model">{t.discoveryModelLabel}</label>
            <select
              id="disc-model"
              disabled={isArchivedProject}
              value={config.model ?? ""}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  model: e.target.value || null,
                }))
              }
            >
              <option value="">{t.evolutionModelDefault}</option>
              {modelOptions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div style={{ marginTop: 12 }}>
          <Button
            type="primary"
            loading={saving}
            disabled={isArchivedProject}
            onClick={() => void saveConfig()}
          >
            {t.discoverySaveConfig}
          </Button>
        </div>
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.discoveryTriggerSectionTitle}</h2>
        </div>

        <div className="discovery-scope-list">
          <label className="evolution-checkbox">
            <input
              type="checkbox"
              disabled={isArchivedProject}
              checked={mineQueryHistory}
              onChange={(e) => setMineQueryHistory(e.target.checked)}
            />
            <span>{t.discoveryScopeMineHistory}</span>
          </label>
          <label className="evolution-checkbox">
            <input
              type="checkbox"
              disabled={isArchivedProject}
              checked={probeRelationships}
              onChange={(e) => setProbeRelationships(e.target.checked)}
            />
            <span>{t.discoveryScopeProbeRelationships}</span>
          </label>
          <label className="evolution-checkbox">
            <input
              type="checkbox"
              disabled={isArchivedProject}
              checked={seedEvalDrafts}
              onChange={(e) => setSeedEvalDrafts(e.target.checked)}
            />
            <span>{t.discoveryScopeSeedEvalDrafts}</span>
          </label>
        </div>

        <div className="discovery-override-block">
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            {t.discoveryBudgetOverridesTitle}
          </div>
          <div className="evolution-config-grid">
            <div className="evolution-field">
              <label htmlFor="disc-override-lookback">
                {`${t.discoveryOverridePrefix}${t.discoveryLookbackDays}`}
              </label>
              <input
                id="disc-override-lookback"
                type="number"
                min={1}
                max={90}
                disabled={isArchivedProject}
                placeholder={String(config.lookback_days)}
                value={overrideLookbackDays}
                onChange={(e) => setOverrideLookbackDays(e.target.value)}
              />
            </div>
            <div className="evolution-field">
              <label htmlFor="disc-override-max-queries">
                {`${t.discoveryOverridePrefix}${t.discoveryMaxQueries}`}
              </label>
              <input
                id="disc-override-max-queries"
                type="number"
                min={50}
                max={20000}
                disabled={isArchivedProject}
                placeholder={String(config.max_queries)}
                value={overrideMaxQueries}
                onChange={(e) => setOverrideMaxQueries(e.target.value)}
              />
            </div>
            <div className="evolution-field">
              <label htmlFor="disc-override-max-fk">
                {`${t.discoveryOverridePrefix}${t.discoveryMaxFkCandidates}`}
              </label>
              <input
                id="disc-override-max-fk"
                type="number"
                min={0}
                max={50}
                disabled={isArchivedProject}
                placeholder={String(config.max_fk_candidates)}
                value={overrideMaxFkCandidates}
                onChange={(e) => setOverrideMaxFkCandidates(e.target.value)}
              />
            </div>
            <div className="evolution-field">
              <label htmlFor="disc-override-sample-limit">
                {`${t.discoveryOverridePrefix}${t.discoverySampleLimit}`}
              </label>
              <input
                id="disc-override-sample-limit"
                type="number"
                min={100}
                max={5000}
                disabled={isArchivedProject}
                placeholder={String(config.sample_limit)}
                value={overrideSampleLimit}
                onChange={(e) => setOverrideSampleLimit(e.target.value)}
              />
            </div>
            <div className="evolution-field">
              <label htmlFor="disc-override-min-ratio">
                {`${t.discoveryOverridePrefix}${t.discoveryMinInclusionRatio}`}
              </label>
              <input
                id="disc-override-min-ratio"
                type="number"
                min={0.5}
                max={1.0}
                step={0.05}
                disabled={isArchivedProject}
                placeholder={String(config.min_inclusion_ratio)}
                value={overrideMinInclusionRatio}
                onChange={(e) => setOverrideMinInclusionRatio(e.target.value)}
              />
            </div>
            <div className="evolution-field">
              <label htmlFor="disc-override-timeout">
                {`${t.discoveryOverridePrefix}${t.discoveryProbeTimeoutSeconds}`}
              </label>
              <input
                id="disc-override-timeout"
                type="number"
                min={5}
                max={120}
                disabled={isArchivedProject}
                placeholder={String(config.probe_timeout_seconds)}
                value={overrideProbeTimeout}
                onChange={(e) => setOverrideProbeTimeout(e.target.value)}
              />
            </div>
          </div>
        </div>

        <div className="discovery-trigger-bar">
          <Button
            type="primary"
            loading={triggering}
            disabled={triggerDisabled}
            onClick={() => void triggerRun()}
          >
            {t.discoveryTriggerButton}
          </Button>
          {dtbConfigured === false && !isArchivedProject && (
            <span className="muted">{t.discoveryTriggerDisabledNoToken}</span>
          )}
          {noScopeSelected && !isArchivedProject && dtbConfigured === true && (
            <span className="muted">{t.discoveryTriggerNeedScope}</span>
          )}
        </div>
      </section>

      <div className="evolution-layout">
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.discoveryRunsTitle}</h2>
            <span className="muted">{runs.length}</span>
          </div>
          {runs.length === 0 ? (
            <div className="empty-state">{t.discoveryEmptyRuns}</div>
          ) : (
            <ul className="correction-list">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    type="button"
                    className={activeRun?.id === run.id ? "active" : ""}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <span className="mono">
                      {formatDateTime(run.created_at)}
                    </span>
                    <Space size={4}>
                      <Tag>
                        {t.discoveryMetricProposals}:{" "}
                        {run.proposals_created ?? 0}
                      </Tag>
                      <Tag color={runStatusColor(run.status)}>
                        {runStatusLabel(run.status, t)}
                      </Tag>
                    </Space>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card section-card">
          {activeRun ? (
            <div className="stack">
              <div className="card-title-row">
                <div>
                  <h2 className="card-title mono">{activeRun.id}</h2>
                  <div className="muted">
                    {formatDateTime(activeRun.started_at)} →{" "}
                    {formatDateTime(activeRun.finished_at)}
                  </div>
                </div>
                <Space size={6} wrap>
                  <Tag color={runStatusColor(activeRun.status)}>
                    {runStatusLabel(activeRun.status, t)}
                  </Tag>
                  {activeRun.mine_query_history && (
                    <Tag>{t.discoveryKindMetric}</Tag>
                  )}
                  {activeRun.probe_relationships && (
                    <Tag>{t.discoveryKindRelationship}</Tag>
                  )}
                  {activeRun.seed_eval_drafts && (
                    <Tag>{t.discoveryKindEvalDraft}</Tag>
                  )}
                </Space>
              </div>

              {activeRun.error && (
                <Alert type="error" title={activeRun.error} showIcon />
              )}

              <div className="discovery-metrics-grid">
                <Tag>
                  {t.discoveryMetricQueriesFetched}:{" "}
                  {activeRun.queries_fetched ?? 0}
                </Tag>
                <Tag>
                  {t.discoveryMetricTemplatesClustered}:{" "}
                  {activeRun.templates_clustered ?? 0}
                </Tag>
                <Tag>
                  {t.discoveryMetricRecalled}:{" "}
                  {activeRun.candidates_recalled ?? 0}
                </Tag>
                <Tag>
                  {t.discoveryMetricProbed}: {activeRun.candidates_probed ?? 0}
                </Tag>
                <Tag color="green">
                  {t.discoveryMetricPassed}: {activeRun.candidates_passed ?? 0}
                </Tag>
                <Tag color="orange">
                  {t.discoveryMetricSkipped}:{" "}
                  {activeRun.candidates_skipped ?? 0}
                </Tag>
                <Tag color="red">
                  {t.discoveryMetricFailed}: {activeRun.candidates_failed ?? 0}
                </Tag>
                <Tag color="blue">
                  {t.discoveryMetricProposals}:{" "}
                  {activeRun.proposals_created ?? 0}
                </Tag>
                <Tag>
                  {t.discoveryMetricEvalDrafts}:{" "}
                  {activeRun.eval_drafts_created ?? 0}
                </Tag>
              </div>

              <div className="stack">
                <h3 className="discovery-section-subtitle">
                  {t.discoveryCandidatesTitle}
                  {detailLoading ? " …" : ` (${activeCandidates.length})`}
                </h3>
                {activeCandidates.length === 0 ? (
                  <div className="empty-state">
                    {t.discoveryEmptyCandidates}
                  </div>
                ) : (
                  activeCandidates.map((candidate) => (
                    <CandidateCard
                      key={candidate.id}
                      candidate={candidate}
                      t={t}
                      onNavigateCorrections={onNavigateCorrections}
                    />
                  ))
                )}
              </div>
            </div>
          ) : (
            <div className="empty-state">{t.discoveryEmptyRuns}</div>
          )}
        </section>
      </div>
    </div>
  );
}
