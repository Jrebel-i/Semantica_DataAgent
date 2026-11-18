import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Space, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { ProjectSelector, storedProject } from "./ProjectSelector";

type EvolutionConfig = components["schemas"]["EvolutionConfigView"];
type EvolutionConfigUpdate = components["schemas"]["EvolutionConfigUpdate"];
type EvolutionRun = components["schemas"]["EvolutionRunView"];
type EvolutionRound = components["schemas"]["EvolutionRoundView"];
type Provider = components["schemas"]["ProviderView"];

const DEFAULT_CONFIG: EvolutionConfig = {
  auto_evolve_enabled: false,
  min_trajectories: 30,
  min_interval_hours: 24,
  max_idle_days: 7,
  max_rounds: 3,
  max_eval_cases: 20,
  evolution_provider_id: null,
  evolution_model: null,
  judge_provider_id: null,
  judge_model: null,
  checkpoint_trajectory_id: null,
  checkpoint_at: null,
  pending_trajectories: 0,
};

async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
  } catch {
    // Ignore non-JSON error payloads
  }
  return fallback;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function runStatusColor(status: EvolutionRun["status"]): string {
  if (status === "accepted") return "green";
  if (status === "running") return "blue";
  return "orange";
}

function decisionColor(decision: string): string {
  if (decision === "accepted") return "green";
  if (decision === "rejected") return "red";
  return "orange";
}

function formatNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function formatL2Summary(l2: Record<string, unknown>): string {
  const mode = typeof l2.mode === "string" ? l2.mode : "-";
  const cases = formatNumber(l2.evaluated_cases);
  if (mode === "cold_start_ab") {
    return `${mode} · cases=${cases} · W_c=${formatNumber(l2.W_c)} W_p=${formatNumber(l2.W_p)} E_c=${formatNumber(l2.E_c)} new_validate_errors=${formatNumber(l2.new_validate_errors)}`;
  }
  if (mode === "confirmed_regression") {
    return `${mode} · cases=${cases} · regressions=${formatNumber(l2.regressions)}`;
  }
  return `${mode} · cases=${cases}`;
}

function ProviderModelPair({
  idPrefix,
  providerLabel,
  modelLabel,
  defaultLabel,
  providerId,
  model,
  providers,
  onProviderChange,
  onModelChange,
}: {
  idPrefix: string;
  providerLabel: string;
  modelLabel: string;
  defaultLabel: string;
  providerId: string | null | undefined;
  model: string | null | undefined;
  providers: Provider[];
  onProviderChange: (nextProviderId: string | null) => void;
  onModelChange: (nextModel: string | null) => void;
}) {
  const selectedProvider = providers.find((p) => p.id === providerId);
  const availableModels = selectedProvider?.models ?? [];
  const modelOptions =
    model && !availableModels.includes(model)
      ? [model, ...availableModels]
      : availableModels;

  return (
    <>
      <div className="evolution-field">
        <label htmlFor={`${idPrefix}-provider`}>{providerLabel}</label>
        <select
          id={`${idPrefix}-provider`}
          value={providerId ?? ""}
          onChange={(e) => onProviderChange(e.target.value || null)}
        >
          <option value="">{defaultLabel}</option>
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name}
            </option>
          ))}
        </select>
      </div>
      <div className="evolution-field">
        <label htmlFor={`${idPrefix}-model`}>{modelLabel}</label>
        <select
          id={`${idPrefix}-model`}
          value={model ?? ""}
          onChange={(e) => onModelChange(e.target.value || null)}
        >
          <option value="">{defaultLabel}</option>
          {modelOptions.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}

function RoundCard({ round, t }: { round: EvolutionRound; t: Messages }) {
  const l1 = round.l1_gate_report as
    | {
        passed?: boolean;
        checks?: { name?: string; passed?: boolean; details?: string[] }[];
        errors?: string[];
      }
    | null
    | undefined;
  const l2 = round.l2_gate_report as Record<string, unknown> | null | undefined;
  const l1Errors = Array.isArray(l1?.errors) ? l1.errors : [];
  const decisionLabel =
    round.decision === "accepted"
      ? t.evolutionDecisionAccepted
      : round.decision === "rejected"
        ? t.evolutionDecisionRejected
        : t.evolutionDecisionError;

  return (
    <article className="evolution-round-card">
      <div className="evolution-round-header">
        <strong>
          {t.evolutionRoundTitle.replace("{n}", String(round.round_number))}
        </strong>
        <Space size={6} wrap>
          <Tag>{round.signal_kind}</Tag>
          <Tag className="mono">{`${round.target_kind}:${round.target}`}</Tag>
          <Tag color={decisionColor(round.decision)}>{decisionLabel}</Tag>
        </Space>
      </div>
      <div className="evolution-round-body">
        <div>
          <span className="muted">{t.evolutionHypothesis}: </span>
          <span>{round.hypothesis}</span>
        </div>
        {(round.evidence_trajectory_ids?.length ?? 0) > 0 && (
          <div className="muted">
            {t.evolutionEvidenceIds}:{" "}
            <span className="mono">
              {round.evidence_trajectory_ids?.join(", ")}
            </span>
          </div>
        )}
        {round.reject_reason && (
          <div className="muted">
            {t.evolutionRejectReason}: {round.reject_reason}
          </div>
        )}
        {l1 && (
          <div className="evolution-gate-block">
            <Space size={8}>
              <span>{t.evolutionL1Gate}</span>
              <Tag color={l1.passed ? "green" : "red"}>
                {l1.passed ? t.evolutionL2Passed : t.evolutionL2Failed}
              </Tag>
            </Space>
            {l1Errors.length > 0 && (
              <ul className="stale-overrides-list">
                {l1Errors.map((err, idx) => (
                  <li key={idx}>{err}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {l2 && (
          <div className="evolution-gate-block">
            <Space size={8}>
              <span>{t.evolutionL2Gate}</span>
              <Tag color={l2.passed ? "green" : "red"}>
                {l2.passed ? t.evolutionL2Passed : t.evolutionL2Failed}
              </Tag>
            </Space>
            <div className="mono muted">{formatL2Summary(l2)}</div>
          </div>
        )}
      </div>
    </article>
  );
}

export function EvolutionPage({
  token,
  messages: t,
  onNavigateCorrections,
}: {
  token: string;
  messages: Messages;
  onNavigateCorrections?: (proposalId?: string) => void;
}) {
  const [project, setProject] = useState(storedProject);
  const projectRef = useRef(project);
  projectRef.current = project;
  const requestGenRef = useRef(0);

  const [providers, setProviders] = useState<Provider[]>([]);
  const [config, setConfig] = useState<EvolutionConfig>(DEFAULT_CONFIG);
  const [runs, setRuns] = useState<EvolutionRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [error, setError] = useState("");
  const [saveNotice, setSaveNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/providers", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as Provider[];
        if (!controller.signal.aborted) {
          setProviders(Array.isArray(data) ? data : []);
        }
      })
      .catch(() => {
        // Ignore abort / optional provider metadata failures
      });
    return () => controller.abort();
  }, [token]);

  const loadProjectData = useCallback(
    async (targetProject: string, signal?: AbortSignal) => {
      const gen = requestGenRef.current;
      const headers = { Authorization: `Bearer ${token}` };
      const encoded = encodeURIComponent(targetProject);
      const [cfgRes, runsRes] = await Promise.all([
        fetch(`/api/v1/projects/${encoded}/evolution-config`, {
          headers,
          signal,
        }),
        fetch(`/api/v1/evolution/runs?project=${encoded}`, {
          headers,
          signal,
        }),
      ]);
      if (!cfgRes.ok) {
        throw new Error(await readErrorDetail(cfgRes, t.error));
      }
      if (!runsRes.ok) {
        throw new Error(await readErrorDetail(runsRes, t.error));
      }
      const nextConfig = (await cfgRes.json()) as EvolutionConfig;
      const nextRuns = (await runsRes.json()) as EvolutionRun[];
      if (
        signal?.aborted ||
        requestGenRef.current !== gen ||
        projectRef.current !== targetProject
      ) {
        return;
      }
      setConfig(nextConfig);
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
    setLoading(true);
    setError("");
    setSaveNotice("");
    void loadProjectData(project, controller.signal)
      .catch((failure) => {
        if (!controller.signal.aborted) {
          setError(failure instanceof Error ? failure.message : t.error);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [loadProjectData, project, t.error]);

  const hasRunning = runs.some((r) => r.status === "running");

  useEffect(() => {
    if (!hasRunning) return;
    const timer = window.setInterval(() => {
      void loadProjectData(projectRef.current).catch(() => {
        // Keep last valid state on transient poll error
      });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [hasRunning, loadProjectData, project]);

  function handleProjectChange(nextProject: string) {
    requestGenRef.current += 1;
    projectRef.current = nextProject;
    setSelectedRunId("");
    setRuns([]);
    setConfig(DEFAULT_CONFIG);
    setError("");
    setSaveNotice("");
    setProject(nextProject);
  }

  async function saveConfig() {
    const targetProject = project;
    setSaving(true);
    setError("");
    setSaveNotice("");
    const payload: EvolutionConfigUpdate = {
      auto_evolve_enabled: config.auto_evolve_enabled,
      min_trajectories: config.min_trajectories,
      min_interval_hours: config.min_interval_hours,
      max_idle_days: config.max_idle_days,
      max_rounds: config.max_rounds,
      max_eval_cases: config.max_eval_cases,
      evolution_provider_id: config.evolution_provider_id ?? null,
      evolution_model: config.evolution_model ?? null,
      judge_provider_id: config.judge_provider_id ?? null,
      judge_model: config.judge_model ?? null,
    };
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(targetProject)}/evolution-config`,
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
      const updated = (await response.json()) as EvolutionConfig;
      if (projectRef.current === targetProject) {
        setConfig(updated);
        setSaveNotice(t.evolutionConfigSaved);
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
    const targetProject = project;
    setTriggering(true);
    setError("");
    try {
      const response = await fetch(
        `/api/v1/evolution/runs?project=${encodeURIComponent(targetProject)}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({}),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const created = (await response.json()) as EvolutionRun;
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

  const selectedRun = useMemo(
    () => runs.find((r) => r.id === selectedRunId) ?? runs[0] ?? null,
    [runs, selectedRunId],
  );

  function statusLabel(status: EvolutionRun["status"]): string {
    if (status === "accepted") return t.evolutionStatusAccepted;
    if (status === "running") return t.evolutionStatusRunning;
    return t.evolutionStatusIncomplete;
  }

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.evolutionTitle}</h1>
          <p className="page-subtitle">{t.evolutionHint}</p>
        </div>
        <Button
          onClick={() => void loadProjectData(projectRef.current)}
          loading={loading}
        >
          {t.refresh}
        </Button>
        <Button
          type="primary"
          onClick={() => void triggerRun()}
          loading={triggering}
          disabled={hasRunning}
        >
          {t.triggerEvolution}
        </Button>
        <ProjectSelector
          token={token}
          value={project}
          onChange={handleProjectChange}
          messages={t}
        />
      </div>

      {error && <Alert type="error" title={error} showIcon />}
      {saveNotice && <Alert type="success" title={saveNotice} showIcon />}

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.evolutionTitle}</h2>
          <Space size={8} wrap>
            <Tag color="blue">
              {t.evolutionPendingTrajectories}: {config.pending_trajectories}
            </Tag>
            <Tag>
              {t.evolutionCheckpoint}: {config.checkpoint_trajectory_id ?? "-"}
            </Tag>
          </Space>
        </div>

        <div className="evolution-config-grid">
          <label className="evolution-checkbox">
            <input
              type="checkbox"
              checked={config.auto_evolve_enabled}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  auto_evolve_enabled: e.target.checked,
                }))
              }
            />
            <span>{t.autoEvolveEnabled}</span>
          </label>

          <div className="evolution-field">
            <label htmlFor="evo-min-trajectories">
              {t.evolutionMinTrajectories}
            </label>
            <input
              id="evo-min-trajectories"
              type="number"
              min={1}
              value={config.min_trajectories}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  min_trajectories: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="evo-min-interval">
              {t.evolutionMinIntervalHours}
            </label>
            <input
              id="evo-min-interval"
              type="number"
              min={1}
              value={config.min_interval_hours}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  min_interval_hours: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="evo-max-idle-days">{t.evolutionMaxIdleDays}</label>
            <input
              id="evo-max-idle-days"
              type="number"
              min={1}
              value={config.max_idle_days}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  max_idle_days: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="evo-max-rounds">{t.evolutionMaxRounds}</label>
            <input
              id="evo-max-rounds"
              type="number"
              min={1}
              value={config.max_rounds}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  max_rounds: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <div className="evolution-field">
            <label htmlFor="evo-max-eval-cases">
              {t.evolutionMaxEvalCases}
            </label>
            <input
              id="evo-max-eval-cases"
              type="number"
              min={1}
              value={config.max_eval_cases}
              onChange={(e) =>
                setConfig((prev) => ({
                  ...prev,
                  max_eval_cases: Number(e.target.value) || 1,
                }))
              }
            />
          </div>

          <ProviderModelPair
            idPrefix="evo-evolver"
            providerLabel={t.evolutionProviderLabel}
            modelLabel={t.evolutionModelLabel}
            defaultLabel={t.evolutionModelDefault}
            providerId={config.evolution_provider_id}
            model={config.evolution_model}
            providers={providers}
            onProviderChange={(next) =>
              setConfig((prev) => ({
                ...prev,
                evolution_provider_id: next,
                evolution_model: null,
              }))
            }
            onModelChange={(next) =>
              setConfig((prev) => ({
                ...prev,
                evolution_model: next,
              }))
            }
          />

          <ProviderModelPair
            idPrefix="evo-judge"
            providerLabel={t.judgeProviderLabel}
            modelLabel={t.judgeModelLabel}
            defaultLabel={t.evolutionModelDefault}
            providerId={config.judge_provider_id}
            model={config.judge_model}
            providers={providers}
            onProviderChange={(next) =>
              setConfig((prev) => ({
                ...prev,
                judge_provider_id: next,
                judge_model: null,
              }))
            }
            onModelChange={(next) =>
              setConfig((prev) => ({
                ...prev,
                judge_model: next,
              }))
            }
          />
        </div>

        <div style={{ marginTop: 12 }}>
          <Button
            type="primary"
            onClick={() => void saveConfig()}
            loading={saving}
          >
            {t.evolutionSaveConfig}
          </Button>
        </div>
      </section>

      <div className="evolution-layout">
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.evolutionRunsTitle}</h2>
            <span className="muted">{runs.length}</span>
          </div>
          {runs.length === 0 ? (
            <div className="empty-state">{t.evolutionEmptyRuns}</div>
          ) : (
            <ul className="correction-list">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    type="button"
                    className={selectedRun?.id === run.id ? "active" : ""}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <span className="mono">
                      {run.trigger_type === "scheduled" ||
                      run.trigger_type === "scheduler"
                        ? t.evolutionTriggerScheduled
                        : t.evolutionTriggerManual}{" "}
                      · {formatDateTime(run.created_at)}
                    </span>
                    <Space size={4}>
                      <Tag>
                        {run.rounds_completed}/{run.max_rounds}
                      </Tag>
                      <Tag color={runStatusColor(run.status)}>
                        {statusLabel(run.status)}
                      </Tag>
                    </Space>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card section-card">
          {selectedRun ? (
            <div className="stack">
              <div className="card-title-row">
                <div>
                  <h2 className="card-title mono">{selectedRun.id}</h2>
                  <div className="muted">
                    {formatDateTime(selectedRun.started_at)} →{" "}
                    {formatDateTime(selectedRun.finished_at)}
                  </div>
                </div>
                <Space size={8} wrap>
                  <Tag color={runStatusColor(selectedRun.status)}>
                    {statusLabel(selectedRun.status)}
                  </Tag>
                  {selectedRun.stop_reason && (
                    <Tag>{`${t.evolutionStopReason}: ${selectedRun.stop_reason}`}</Tag>
                  )}
                  {selectedRun.proposal_id && (
                    <Button
                      type="primary"
                      size="small"
                      onClick={() =>
                        onNavigateCorrections?.(
                          selectedRun.proposal_id ?? undefined,
                        )
                      }
                    >
                      {t.viewEvolutionProposal}
                    </Button>
                  )}
                </Space>
              </div>

              {selectedRun.error && (
                <Alert type="error" title={selectedRun.error} showIcon />
              )}

              <Space size={8} wrap>
                <Tag>
                  {t.evolutionDiagnosisPool}:{" "}
                  {selectedRun.diagnosis_trajectory_ids?.length ?? 0}
                </Tag>
                <Tag>
                  {t.evolutionReservePool}:{" "}
                  {selectedRun.reserve_trajectory_ids?.length ?? 0}
                </Tag>
                <Tag>
                  {t.evolutionCheckpoint}:{" "}
                  {selectedRun.checkpoint_before_trajectory_id ?? "-"} →{" "}
                  {selectedRun.checkpoint_after_trajectory_id ?? "-"}
                </Tag>
              </Space>

              <div className="stack">
                {(selectedRun.rounds ?? []).map((round) => (
                  <RoundCard key={round.id} round={round} t={t} />
                ))}
              </div>
            </div>
          ) : (
            <div className="empty-state">{t.evolutionEmptyRuns}</div>
          )}
        </section>
      </div>
    </section>
  );
}
