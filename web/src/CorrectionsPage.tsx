import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { components } from "./api";
import { readErrorDetail } from "./httpErrors";
import { DiffViewer } from "./DiffViewer";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import { ProjectSelector, storedProject } from "./ProjectSelector";
import { formatOverlapRatio, formatProbePartitions } from "./DiscoveryPanel";
import { TableScroll } from "./TableScroll";
import { CorrectionDraftDrawer } from "./chat/CorrectionDraftDrawer";

function formatRegressions(regressions: unknown): string {
  if (Array.isArray(regressions)) {
    const ids = regressions.map(String).filter(Boolean);
    return ids.length > 0 ? `${ids.length} (${ids.join(", ")})` : "0";
  }
  if (typeof regressions === "number" && Number.isFinite(regressions)) {
    return String(regressions);
  }
  return "0";
}

type L2GateReport = {
  passed?: boolean;
  mode?: string;
  evaluated_cases?: number;
  regressions?: string[] | number;
  W_c?: number;
  W_p?: number;
  E_c?: number;
  new_validate_errors?: number;
  errors?: string[];
};

type ProvenanceReport = {
  evolution_run_id?: string;
  round_number?: number;
  signal_kind?: string;
  hypothesis?: string;
  evidence_trajectory_ids?: number[];
};

type SqlClusterItem = {
  fingerprint?: string;
  template_sql?: string;
  tables?: string[];
  execution_count?: number;
  distinct_callers?: number;
  dialect?: string;
};

type FkProbeEvidenceItem = {
  from_table?: string;
  from_columns?: string[];
  to_table?: string;
  to_columns?: string[];
  sample_distinct_count?: number | null;
  matched_count?: number | null;
  overlap_ratio?: number | null;
  inclusion_rate?: number | null;
  join_matched?: boolean | null;
  join_hit?: boolean | null;
  probe_partitions?: Record<string, Record<string, string>>;
  probing_status?: string;
};

type TraceExcerptMessage = {
  role?: string;
  content?: string;
};

type TraceMessageSummary = {
  trace_id?: string;
  started_at?: string | null;
  messages?: TraceExcerptMessage[];
};

type TriggerEvidence = {
  question?: string;
  answer?: string;
  description?: string;
  conversation_id?: string | null;
  turn_id?: number | null;
  knowledge_gap_id?: string | null;
  evolution_run_id?: string;
  trace_source_plugin?: string;
  trace_ids?: string[];
  session_ids?: string[];
  window_since?: string | null;
  window_until?: string | null;
  conversation_count?: number;
  message_summary?: TraceMessageSummary[];
  run_id?: string;
  discovery_run_id?: string;
  candidate_kind?: string;
  from_table?: string;
  from_columns?: string[];
  to_table?: string;
  to_columns?: string[];
  recall_sources?: string[];
  recall_score?: number;
  query_history_frequency?: number;
  distinct_callers?: number;
  sample_distinct_count?: number | null;
  matched_count?: number | null;
  overlap_ratio?: number | null;
  inclusion_rate?: number | null;
  join_matched?: boolean | null;
  join_hit?: boolean | null;
  probe_partitions?: Record<string, Record<string, string>>;
  probing_status?: string;
  topic?: string;
  expression?: string;
  predicate?: string;
  template_count?: number;
  co_occurrence_ratio?: number;
  sql_clusters?: SqlClusterItem[];
  fk_probe_evidence?: FkProbeEvidenceItem[];
  llm_rationale?: string;
};

type UnifiedEvidence = {
  source_type?: string;
  summary_reason?: string;
  trigger_evidence?: TriggerEvidence;
};

type GateReport = {
  passed?: boolean;
  checks?: { name?: string; passed?: boolean; details?: string[] }[];
  errors?: string[];
  l1_gate?: {
    passed?: boolean;
    checks?: { name?: string; passed?: boolean; details?: string[] }[];
    errors?: string[];
  };
  l2_gate?: L2GateReport;
  provenance?: ProvenanceReport;
  discovery_evidence?: TriggerEvidence;
  evidence?: UnifiedEvidence;
};

type Proposal = {
  id: string;
  target: string;
  reason: string;
  diff: string;
  patch: string;
  status: string;
  source?: string;
  target_kind?: string;
  is_new_file?: boolean;
  structured_patch?: Record<string, unknown> | null;
  gate_report?: GateReport | null;
  evidence?: UnifiedEvidence | null;
};

function initialPatchForProposal(row: Proposal): string {
  if (row.patch && row.patch.trim()) return row.patch;
  if (row.structured_patch)
    return JSON.stringify(row.structured_patch, null, 2);
  return "";
}

type EvalQuestion = components["schemas"]["EvalQuestionView"];
type Gap = {
  id: string;
  project_id: string;
  question: string;
  missing: string;
  status: string;
  can_review: boolean;
  created_at: string;
  reviewed_at?: string | null;
  reviewer_name?: string | null;
};

function gapDescription(question: string, missing: string): string {
  const q = Array.from(question);
  const m = Array.from(missing);
  const budget = 4999;
  const qLimit = Math.min(
    q.length,
    Math.max(Math.floor(budget / 2), budget - m.length),
  );
  return `${q.slice(0, qLimit).join("")}\n${m.slice(0, budget - qLimit).join("")}`;
}
type StatusFilter = "all" | "pending" | "resolved";
type SourceFilter = "all" | "manual" | "mcp" | "evolution" | "discovery";

function statusMeta(
  status: string,
  t: Messages,
): { label: string; color: string } {
  if (status === "pending") return { label: t.statusPending, color: "gold" };
  if (status === "approved") return { label: t.statusApproved, color: "green" };
  if (status === "auto_applied")
    return { label: t.statusAutoApplied, color: "cyan" };
  if (status === "reverted")
    return { label: t.statusReverted, color: "default" };
  if (status === "rejected") return { label: t.statusRejected, color: "red" };
  return { label: status, color: "default" };
}

function sourceLabel(source: string | undefined, t: Messages): string {
  if (source === "mcp") return t.sourceMcp;
  if (source === "evolution") return t.sourceEvolution;
  if (source === "discovery") return t.sourceDiscovery;
  if (source === "manual") return t.sourceManual;
  return source || "";
}

function isNewFileProposal(row: Proposal): boolean {
  return (
    Boolean(row.is_new_file) ||
    (row.target_kind === "document" && row.diff.startsWith("--- /dev/null"))
  );
}

function formatEvidenceTime(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function discoveryKindText(kind: string | undefined, t: Messages): string {
  if (kind === "relationship") return t.discoveryKindRelationship;
  if (kind === "metric") return t.discoveryKindMetric;
  if (kind === "filter_rule") return t.discoveryKindFilterRule;
  if (kind === "table") return t.discoveryKindTable;
  if (kind === "field") return t.discoveryKindField;
  if (kind === "override") return t.discoveryKindOverride;
  if (kind === "document") return t.discoveryKindDocument;
  if (kind === "synthesized") return t.discoveryKindSynthesized;
  if (kind === "eval_draft") return t.discoveryKindEvalDraft;
  return kind || "-";
}

function ProposalEvidenceCard({
  proposal,
  t,
}: {
  proposal: Proposal;
  t: Messages;
}) {
  const unified = proposal.evidence ?? proposal.gate_report?.evidence ?? null;
  const legacyDisc = proposal.gate_report?.discovery_evidence;
  const legacyProv = proposal.gate_report?.provenance;
  const trigger: TriggerEvidence = {
    ...(legacyDisc ?? {}),
    ...(unified?.trigger_evidence ?? {}),
  };
  const sourceType = unified?.source_type || proposal.source || "manual";
  const summaryReason = unified?.summary_reason || proposal.reason;
  const runId =
    trigger.run_id ||
    trigger.discovery_run_id ||
    trigger.evolution_run_id ||
    legacyProv?.evolution_run_id;
  const overlapRatio =
    typeof trigger.overlap_ratio === "number"
      ? trigger.overlap_ratio
      : typeof trigger.inclusion_rate === "number"
        ? trigger.inclusion_rate
        : null;
  const overlapText = formatOverlapRatio(overlapRatio);
  const partitionsText = formatProbePartitions(trigger.probe_partitions);
  const joinMatched =
    typeof trigger.join_matched === "boolean"
      ? trigger.join_matched
      : typeof trigger.join_hit === "boolean"
        ? trigger.join_hit
        : null;
  const sqlClusters = Array.isArray(trigger.sql_clusters)
    ? trigger.sql_clusters
    : [];
  const fkProbes = Array.isArray(trigger.fk_probe_evidence)
    ? trigger.fk_probe_evidence
    : [];
  const traceIds = Array.isArray(trigger.trace_ids) ? trigger.trace_ids : [];
  const messageSummary = Array.isArray(trigger.message_summary)
    ? trigger.message_summary
    : [];

  const hasDetail = Boolean(
    trigger.question ||
    trigger.description ||
    trigger.conversation_id ||
    trigger.knowledge_gap_id ||
    runId ||
    traceIds.length > 0 ||
    trigger.window_since ||
    trigger.window_until ||
    messageSummary.length > 0 ||
    typeof trigger.query_history_frequency === "number" ||
    sqlClusters.length > 0 ||
    fkProbes.length > 0 ||
    overlapText ||
    legacyDisc ||
    legacyProv,
  );

  if (!unified && !legacyDisc && proposal.source !== "discovery") {
    return null;
  }

  return (
    <div className="discovery-evidence-card">
      <div className="discovery-evidence-header">
        <strong>
          {sourceType === "discovery"
            ? t.discoveryEvidenceTitle
            : t.proposalEvidenceTitle}
        </strong>
        <Space size={6} wrap>
          <Tag>{sourceLabel(sourceType, t)}</Tag>
          {trigger.trace_source_plugin && (
            <Tag className="mono">
              {t.evidenceTracePlugin}: {trigger.trace_source_plugin}
            </Tag>
          )}
          {trigger.candidate_kind && (
            <Tag>{discoveryKindText(trigger.candidate_kind, t)}</Tag>
          )}
          {runId && (
            <Tag className="mono">
              {t.discoveryEvidenceRunId}: {runId}
            </Tag>
          )}
        </Space>
      </div>

      {summaryReason && (
        <div>
          <span className="muted">{t.proposalSummaryReason}: </span>
          <span>{summaryReason}</span>
        </div>
      )}

      {!hasDetail && sourceType === "discovery" && (
        <div className="muted">{t.discoveryEvidenceNoDetail}</div>
      )}

      {trigger.question && (
        <div>
          <span className="muted">{t.evidenceQuestion}: </span>
          <span>{trigger.question}</span>
        </div>
      )}

      {trigger.description && trigger.description !== summaryReason && (
        <div>
          <span className="muted">{t.evidenceDescription}: </span>
          <span>{trigger.description}</span>
        </div>
      )}

      {(trigger.conversation_id || trigger.knowledge_gap_id) && (
        <div className="discovery-evidence-pills">
          {trigger.conversation_id && (
            <Tag className="mono">
              {t.evidenceConversation}: {trigger.conversation_id}
              {typeof trigger.turn_id === "number" ? `#${trigger.turn_id}` : ""}
            </Tag>
          )}
          {trigger.knowledge_gap_id && (
            <Tag className="mono">
              {t.evidenceKnowledgeGap}: {trigger.knowledge_gap_id}
            </Tag>
          )}
        </div>
      )}

      {(trigger.window_since ||
        trigger.window_until ||
        typeof trigger.conversation_count === "number" ||
        traceIds.length > 0) && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="discovery-evidence-pills">
            {(trigger.window_since || trigger.window_until) && (
              <Tag className="mono">
                {t.evidenceTraceWindow}:{" "}
                {formatEvidenceTime(trigger.window_since)} →{" "}
                {formatEvidenceTime(trigger.window_until)}
              </Tag>
            )}
            {typeof trigger.conversation_count === "number" && (
              <Tag>
                {t.evidenceTraceCount}: {trigger.conversation_count}
              </Tag>
            )}
            {traceIds.length > 0 && (
              <Tag className="mono">
                {t.evidenceTraceIds}: {traceIds.slice(0, 5).join(", ")}
                {traceIds.length > 5 ? ` (+${traceIds.length - 5})` : ""}
              </Tag>
            )}
          </div>
          {messageSummary.length > 0 && (
            <div className="evidence-excerpt-list">
              <strong>{t.evidenceTraceExcerpts}</strong>
              {messageSummary.slice(0, 3).map((item, idx) => (
                <div
                  key={item.trace_id || idx}
                  className="evidence-excerpt-item"
                >
                  <div className="mono muted">
                    {item.trace_id || `#${idx + 1}`}{" "}
                    {item.started_at
                      ? `· ${formatEvidenceTime(item.started_at)}`
                      : ""}
                  </div>
                  {(item.messages ?? []).slice(0, 4).map((msg, msgIdx) => (
                    <div key={msgIdx}>
                      <strong className="mono">[{msg.role}]: </strong>
                      <span>{msg.content}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="discovery-evidence-pills">
        {typeof trigger.query_history_frequency === "number" && (
          <Tag>
            {t.discoveryEvidenceFrequency}: {trigger.query_history_frequency}
          </Tag>
        )}
        {typeof trigger.distinct_callers === "number" && (
          <Tag>
            {t.discoveryEvidenceDistinctCallers}: {trigger.distinct_callers}
          </Tag>
        )}
        {typeof trigger.sample_distinct_count === "number" && (
          <Tag>
            {t.discoveryEvidenceSampleCount}: {trigger.sample_distinct_count}
          </Tag>
        )}
        {typeof trigger.matched_count === "number" && (
          <Tag>
            {t.discoveryEvidenceMatchedCount}: {trigger.matched_count}
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
        {trigger.probing_status &&
          trigger.probing_status !== "not_applicable" && (
            <Tag className="mono">
              {t.discoveryEvidenceProbingStatus}: {trigger.probing_status}
            </Tag>
          )}
      </div>

      {partitionsText && (
        <div className="muted mono">
          {t.discoveryEvidencePartitions}: {partitionsText}
        </div>
      )}
      {trigger.expression && (
        <div className="muted mono">{trigger.expression}</div>
      )}
      {trigger.predicate && (
        <div className="muted mono">{trigger.predicate}</div>
      )}

      {sqlClusters.length > 0 && (
        <div className="evidence-cluster-list">
          <strong>{t.evidenceSqlClusters}</strong>
          {sqlClusters.slice(0, 3).map((cluster, idx) => (
            <div
              key={cluster.fingerprint || idx}
              className="evidence-cluster-item"
            >
              <Space size={6} wrap>
                {typeof cluster.execution_count === "number" && (
                  <Tag>
                    {t.evidenceClusterExecs.replace(
                      "{count}",
                      String(cluster.execution_count),
                    )}
                  </Tag>
                )}
                {typeof cluster.distinct_callers === "number" && (
                  <Tag>
                    {t.evidenceClusterCallers.replace(
                      "{count}",
                      String(cluster.distinct_callers),
                    )}
                  </Tag>
                )}
                {cluster.dialect && <Tag>{cluster.dialect}</Tag>}
                {Array.isArray(cluster.tables) && cluster.tables.length > 0 && (
                  <span className="mono muted">
                    {cluster.tables.join(", ")}
                  </span>
                )}
              </Space>
              {cluster.template_sql && (
                <pre className="mono evidence-sql-preview">
                  {cluster.template_sql}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      {fkProbes.length > 0 && (
        <div className="evidence-cluster-list">
          <strong>{t.evidenceFkProbes}</strong>
          {fkProbes.map((fk, idx) => {
            const fkRate = formatOverlapRatio(
              typeof fk.overlap_ratio === "number"
                ? fk.overlap_ratio
                : typeof fk.inclusion_rate === "number"
                  ? fk.inclusion_rate
                  : null,
            );
            return (
              <div key={idx} className="evidence-cluster-item">
                <span className="mono">
                  {fk.from_table}({(fk.from_columns ?? []).join(", ")}) →{" "}
                  {fk.to_table}({(fk.to_columns ?? []).join(", ")})
                </span>
                <Space size={6} wrap>
                  {fkRate && (
                    <Tag color="blue">
                      {t.discoveryEvidenceOverlapRatio}: {fkRate}
                    </Tag>
                  )}
                  {typeof fk.sample_distinct_count === "number" && (
                    <Tag>
                      {t.discoveryEvidenceSampleCount}:{" "}
                      {fk.sample_distinct_count}
                    </Tag>
                  )}
                  {typeof fk.matched_count === "number" && (
                    <Tag>
                      {t.discoveryEvidenceMatchedCount}: {fk.matched_count}
                    </Tag>
                  )}
                </Space>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function CorrectionsPage({
  token,
  gaps = false,
  messages: t,
  initialProposalId,
}: {
  token: string;
  gaps?: boolean;
  messages: Messages;
  initialProposalId?: string | null;
}) {
  const [project, setProject] = useState(storedProject);
  const [activeTab, setActiveTab] = useState<"proposals" | "eval">("proposals");
  const [rows, setRows] = useState<Proposal[]>([]);
  const [missing, setMissing] = useState<Gap[]>([]);
  const [missingProject, setMissingProject] = useState("");
  const [draftGap, setDraftGap] = useState<Gap | null>(null);
  const currentProject = useRef(project);
  const requestGeneration = useRef(0);
  const projectGeneration = useRef(0);
  if (currentProject.current !== project) {
    requestGeneration.current++;
    projectGeneration.current++;
  }
  currentProject.current = project;
  const [selected, setSelected] = useState<Proposal | null>(null);
  const targetProposalId =
    initialProposalId ??
    new URLSearchParams(window.location.search).get("proposal_id");
  useEffect(() => {
    if (!targetProposalId || rows.length === 0) return;
    const match = rows.find((row) => row.id === targetProposalId);
    if (match) {
      setActiveTab("proposals");
      setSelected(match);
      setPatch(initialPatchForProposal(match));
    }
  }, [rows, targetProposalId]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [patch, setPatch] = useState("");
  const [error, setError] = useState("");
  const [reverting, setReverting] = useState(false);

  // Eval questions state (lazy-loaded on tab open)
  const [evalQuestions, setEvalQuestions] = useState<EvalQuestion[]>([]);
  const [evalSplitFilter, setEvalSplitFilter] = useState<
    "all" | "dev" | "holdout"
  >("all");
  const [evalConfirmedFilter, setEvalConfirmedFilter] = useState<
    "all" | "confirmed" | "unconfirmed"
  >("all");
  const [evalError, setEvalError] = useState("");
  const [evalModalOpen, setEvalModalOpen] = useState(false);
  const [editingEval, setEditingEval] = useState<EvalQuestion | null>(null);
  const [eqQuestion, setEqQuestion] = useState("");
  const [eqCaliber, setEqCaliber] = useState("");
  const [eqSql, setEqSql] = useState("");
  const [eqCategory, setEqCategory] = useState("单表聚合");
  const [eqDialect, setEqDialect] = useState<"hive" | "spark" | "doris">(
    "hive",
  );
  const [eqSplit, setEqSplit] = useState<"dev" | "holdout">("dev");
  const [eqConfirmed, setEqConfirmed] = useState(true);
  const [eqSaving, setEqSaving] = useState(false);

  const projectQuery = `project=${encodeURIComponent(project)}`;

  const sourceScopedRows = rows.filter((row) => {
    if (sourceFilter === "all") return true;
    return (row.source || "manual") === sourceFilter;
  });
  const pendingCount = sourceScopedRows.filter(
    (row) => row.status === "pending",
  ).length;
  const resolvedCount = sourceScopedRows.length - pendingCount;
  const filteredRows = sourceScopedRows.filter((row) => {
    if (statusFilter === "pending") return row.status === "pending";
    if (statusFilter === "resolved") return row.status !== "pending";
    return true;
  });
  const selectedMeta = selected ? statusMeta(selected.status, t) : null;

  const refresh = useCallback(async () => {
    const generation = ++requestGeneration.current;
    try {
      const response = await fetch(
        `${
          gaps ? "/api/v1/knowledge-gaps" : "/api/v1/corrections"
        }?${projectQuery}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (
        currentProject.current !== project ||
        generation !== requestGeneration.current
      )
        return;
      if (gaps) {
        setMissing(data as Gap[]);
        setMissingProject(project);
      } else setRows(data as Proposal[]);
      setError("");
    } catch {
      if (
        currentProject.current === project &&
        generation === requestGeneration.current
      )
        setError(t.correctionLoadError);
    }
  }, [gaps, project, projectQuery, token, t.correctionLoadError]);

  const loadEvalQuestions = useCallback(async () => {
    if (gaps) return;
    try {
      const response = await fetch(`/api/v1/eval-questions?${projectQuery}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalLoadError));
      }
      setEvalQuestions((await response.json()) as EvalQuestion[]);
      setEvalError("");
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalLoadError,
      );
    }
  }, [gaps, projectQuery, token, t.evalLoadError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    setSelected(null);
    setDraftGap(null);
    setError("");
  }, [project]);

  async function reviewGap(gap: Gap, status: "confirmed" | "rejected") {
    if (gap.project_id !== project) return;
    const generation = projectGeneration.current;
    try {
      const response = await fetch(
        `/api/v1/knowledge-gaps/${encodeURIComponent(gap.id)}/review?${projectQuery}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ status }),
        },
      );
      if (response.status === 409) {
        if (projectGeneration.current === generation) {
          await refresh();
          if (projectGeneration.current === generation)
            setError(t.gapReviewConflict);
        }
        return;
      }
      if (!response.ok)
        throw new Error(
          await readErrorDetail(response, t.correctionReviewError),
        );
      if (projectGeneration.current === generation) await refresh();
    } catch (failure) {
      if (projectGeneration.current === generation)
        setError(
          failure instanceof Error ? failure.message : t.correctionReviewError,
        );
    }
  }

  useEffect(() => {
    if (!gaps && activeTab === "eval") {
      void loadEvalQuestions();
    }
  }, [activeTab, gaps, loadEvalQuestions]);

  async function review(action: "approve" | "reject", edited = false) {
    if (!selected) return;
    try {
      let bodyPayload:
        | { patch: string; structured_patch?: Record<string, unknown> }
        | undefined;
      if (action === "approve" && edited) {
        bodyPayload = { patch };
        const isStructured =
          Boolean(selected.structured_patch) ||
          selected.target_kind === "override";
        const trimmed = patch.trim();
        if (isStructured && trimmed.startsWith("{")) {
          try {
            const parsed = JSON.parse(trimmed) as unknown;
            if (
              parsed &&
              typeof parsed === "object" &&
              !Array.isArray(parsed)
            ) {
              bodyPayload = {
                patch,
                structured_patch: parsed as Record<string, unknown>,
              };
            }
          } catch {
            // Fall through so backend returns a 422 validation error on malformed JSON/YAML
          }
        }
      }
      const response = await fetch(
        `/api/v1/corrections/${selected.id}/${action}?${projectQuery}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: bodyPayload ? JSON.stringify(bodyPayload) : undefined,
        },
      );
      if (!response.ok) {
        throw new Error(
          await readErrorDetail(response, t.correctionReviewError),
        );
      }
      setSelected(null);
      setError("");
      await refresh();
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : t.correctionReviewError,
      );
    }
  }

  async function revertSelected() {
    if (!selected) return;
    setReverting(true);
    try {
      const response = await fetch(
        `/api/v1/corrections/${selected.id}/revert?${projectQuery}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(
          await readErrorDetail(response, t.correctionRevertError),
        );
      }
      const updated = (await response.json()) as Partial<Proposal>;
      setSelected((prev) =>
        prev ? { ...prev, ...updated } : (updated as Proposal),
      );
      setError("");
      await refresh();
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : t.correctionRevertError,
      );
    } finally {
      setReverting(false);
    }
  }

  function openCreateEvalModal() {
    setEditingEval(null);
    setEqQuestion("");
    setEqCaliber("");
    setEqSql("");
    setEqCategory("单表聚合");
    setEqDialect("hive");
    setEqSplit("dev");
    setEqConfirmed(true);
    setEvalModalOpen(true);
  }

  function openEditEvalModal(row: EvalQuestion) {
    setEditingEval(row);
    setEqQuestion(row.question);
    setEqCaliber(row.caliber || "");
    setEqSql(row.reference_sql || "");
    setEqCategory(row.category || "单表聚合");
    setEqDialect(
      row.dialect === "spark" || row.dialect === "doris" ? row.dialect : "hive",
    );
    setEqSplit(row.split === "holdout" ? "holdout" : "dev");
    setEqConfirmed(row.answer_confirmed);
    setEvalModalOpen(true);
  }

  async function saveEvalQuestion() {
    setEqSaving(true);
    setEvalError("");
    try {
      const url = editingEval
        ? `/api/v1/eval-questions/${encodeURIComponent(editingEval.id)}?${projectQuery}`
        : `/api/v1/eval-questions?${projectQuery}`;
      const response = await fetch(url, {
        method: editingEval ? "PATCH" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: eqQuestion,
          caliber: eqCaliber,
          reference_sql: eqSql,
          category: eqCategory,
          dialect: eqDialect,
          split: eqSplit,
          answer_confirmed: eqConfirmed,
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      setEvalModalOpen(false);
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    } finally {
      setEqSaving(false);
    }
  }

  async function confirmEvalQuestion(row: EvalQuestion) {
    setEvalError("");
    try {
      const response = await fetch(
        `/api/v1/eval-questions/${encodeURIComponent(row.id)}?${projectQuery}`,
        {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ answer_confirmed: true }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    }
  }

  async function deleteEvalQuestion(row: EvalQuestion) {
    setEvalError("");
    try {
      const response = await fetch(
        `/api/v1/eval-questions/${encodeURIComponent(row.id)}?${projectQuery}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    }
  }

  const filteredEvalQuestions = evalQuestions.filter((row) => {
    if (evalSplitFilter !== "all" && row.split !== evalSplitFilter)
      return false;
    if (evalConfirmedFilter === "confirmed" && !row.answer_confirmed)
      return false;
    if (evalConfirmedFilter === "unconfirmed" && row.answer_confirmed)
      return false;
    return true;
  });

  const canRevert =
    selected?.status === "approved" || selected?.status === "auto_applied";

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {gaps ? t.gapTitle : t.correctionTitle}
          </h1>
          <p className="page-subtitle">{gaps ? t.gapHint : t.correctionHint}</p>
        </div>
        <button
          type="button"
          onClick={() =>
            void (activeTab === "eval" && !gaps
              ? loadEvalQuestions()
              : refresh())
          }
        >
          {t.refresh}
        </button>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
          includeMemberProjects={gaps}
        />
      </div>
      {error && <Alert type="error" title={error} />}
      {gaps ? (
        missingProject !== project || missing.length === 0 ? (
          <div className="empty-state">{t.emptyContext}</div>
        ) : (
          missing.map((gap) => (
            <article className="gap-card" key={gap.id}>
              <h3>{gap.question}</h3>
              <p>{gap.missing}</p>
              <p>
                {t.gapCreated}:{" "}
                {gap.created_at
                  ? new Date(gap.created_at).toLocaleString()
                  : ""}
              </p>
              <p>
                {gap.status === "confirmed"
                  ? t.gapConfirmed
                  : gap.status === "rejected"
                    ? t.gapReject
                    : t.statusPending}
              </p>
              {gap.reviewed_at && (
                <p>
                  {t.gapReviewed}: {new Date(gap.reviewed_at).toLocaleString()}{" "}
                  {gap.reviewer_name}
                </p>
              )}
              {gap.status === "pending" && gap.can_review && (
                <Space>
                  <Button onClick={() => void reviewGap(gap, "confirmed")}>
                    {t.gapConfirm}
                  </Button>
                  <Button onClick={() => void reviewGap(gap, "rejected")}>
                    {t.gapReject}
                  </Button>
                </Space>
              )}
              {gap.status === "confirmed" && (
                <Space>
                  <Button onClick={() => setDraftGap(gap)}>
                    {t.gapCorrection}
                  </Button>
                  {gap.can_review && (
                    <a
                      href={`/source?project=${encodeURIComponent(gap.project_id)}&gap=${encodeURIComponent(gap.id)}`}
                    >
                      {t.gapAddKnowledge}
                    </a>
                  )}
                </Space>
              )}
            </article>
          ))
        )
      ) : (
        <>
          <div className="correction-filters" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === "proposals"}
              className={activeTab === "proposals" ? "active" : ""}
              onClick={() => setActiveTab("proposals")}
            >
              {t.correctionProposalsTab}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === "eval"}
              className={activeTab === "eval" ? "active" : ""}
              onClick={() => setActiveTab("eval")}
            >
              {t.evalQuestionsTab}
            </button>
          </div>
          {activeTab === "proposals" ? (
            <div className="correction-layout">
              <section className="card section-card">
                <div className="card-title-row">
                  <h2 className="card-title">{t.correctionTitle}</h2>
                  <span className="muted">{filteredRows.length}</span>
                </div>
                {rows.length > 0 && (
                  <div className="stack" style={{ gap: 8 }}>
                    <div className="correction-filters" role="group">
                      {(
                        [
                          [
                            "all",
                            `${t.filterAll} (${sourceScopedRows.length})`,
                          ],
                          ["pending", `${t.filterPending} (${pendingCount})`],
                          [
                            "resolved",
                            `${t.filterResolved} (${resolvedCount})`,
                          ],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          className={statusFilter === key ? "active" : ""}
                          aria-pressed={statusFilter === key}
                          onClick={() => setStatusFilter(key)}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <div className="correction-filters" role="group">
                      {(
                        [
                          ["all", t.sourceFilterAll],
                          ["manual", t.sourceManual],
                          ["mcp", t.sourceMcp],
                          ["evolution", t.sourceEvolution],
                          ["discovery", t.sourceDiscovery],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          className={sourceFilter === key ? "active" : ""}
                          aria-pressed={sourceFilter === key}
                          onClick={() => setSourceFilter(key)}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {filteredRows.length === 0 ? (
                  <div className="empty-state">{t.emptyContext}</div>
                ) : (
                  <ul className="correction-list">
                    {filteredRows.map((row) => {
                      const meta = statusMeta(row.status, t);
                      const srcText = row.source
                        ? sourceLabel(row.source, t)
                        : "";
                      const targetLabel = safeDecodePath(row.target);
                      return (
                        <li key={row.id}>
                          <button
                            type="button"
                            className={selected?.id === row.id ? "active" : ""}
                            title={targetLabel}
                            onClick={() => {
                              setSelected(row);
                              setPatch(initialPatchForProposal(row));
                            }}
                          >
                            <span className="mono" title={targetLabel}>
                              {targetLabel}
                            </span>
                            <Space size={4}>
                              {isNewFileProposal(row) && (
                                <Tag color="purple">{t.draftNewFileBadge}</Tag>
                              )}
                              {srcText && <Tag>{srcText}</Tag>}
                              <Tag color={meta.color}>{meta.label}</Tag>
                            </Space>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
              <section className="card section-card">
                {selected ? (
                  <>
                    <div className="card-title-row">
                      <h2 className="card-title mono">
                        {safeDecodePath(selected.target)}
                      </h2>
                      <Space size={6}>
                        {isNewFileProposal(selected) && (
                          <Tag color="purple">{t.draftNewFileBadge}</Tag>
                        )}
                        {selected.source && (
                          <Tag>{sourceLabel(selected.source, t)}</Tag>
                        )}
                        {selected.target_kind && (
                          <Tag>{selected.target_kind}</Tag>
                        )}
                        {selectedMeta && (
                          <Tag color={selectedMeta.color}>
                            {selectedMeta.label}
                          </Tag>
                        )}
                      </Space>
                    </div>
                    <p className="muted">{selected.reason}</p>
                    {selected.gate_report && (
                      <>
                        <Alert
                          style={{ marginBottom: 12 }}
                          type={
                            selected.gate_report.passed ? "success" : "error"
                          }
                          showIcon
                          title={
                            selected.gate_report.passed
                              ? t.gatePassed
                              : t.gateFailed
                          }
                          description={
                            (selected.gate_report.errors?.length ?? 0) > 0 ? (
                              <ul className="stale-overrides-list">
                                {selected.gate_report.errors?.map((err, i) => (
                                  <li key={i}>{err}</li>
                                ))}
                              </ul>
                            ) : undefined
                          }
                        />
                        {(selected.gate_report.provenance ||
                          selected.gate_report.l2_gate) && (
                          <div className="evolution-gate-meta">
                            {selected.gate_report.provenance && (
                              <div>
                                <strong>{t.evolutionProposalProvenance}</strong>
                                <div className="muted mono">
                                  {selected.gate_report.provenance
                                    .evolution_run_id ?? "-"}{" "}
                                  · #
                                  {selected.gate_report.provenance
                                    .round_number ?? "-"}{" "}
                                  ·{" "}
                                  {selected.gate_report.provenance
                                    .signal_kind ?? "-"}
                                </div>
                                {selected.gate_report.provenance.hypothesis && (
                                  <div>
                                    {selected.gate_report.provenance.hypothesis}
                                  </div>
                                )}
                              </div>
                            )}
                            {selected.gate_report.l2_gate && (
                              <div>
                                <strong>{t.evolutionL2Gate}</strong>
                                <div className="muted mono">
                                  {selected.gate_report.l2_gate.mode ?? "-"} ·
                                  cases=
                                  {selected.gate_report.l2_gate
                                    .evaluated_cases ?? 0}
                                  {selected.gate_report.l2_gate.mode ===
                                  "cold_start_ab"
                                    ? ` · W_c=${selected.gate_report.l2_gate.W_c ?? 0} W_p=${selected.gate_report.l2_gate.W_p ?? 0} E_c=${selected.gate_report.l2_gate.E_c ?? 0}`
                                    : selected.gate_report.l2_gate
                                          .regressions !== undefined
                                      ? ` · regressions=${formatRegressions(selected.gate_report.l2_gate.regressions)}`
                                      : ""}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </>
                    )}
                    <ProposalEvidenceCard proposal={selected} t={t} />
                    <DiffViewer
                      diff={selected.diff}
                      messages={t}
                      title={safeDecodePath(selected.target)}
                      defaultMode="split"
                    />
                    {canRevert && (
                      <div className="save-bar" style={{ marginTop: 12 }}>
                        <button
                          type="button"
                          className="danger"
                          disabled={reverting}
                          onClick={() => void revertSelected()}
                        >
                          {t.correctionRevert}
                        </button>
                      </div>
                    )}
                    {selected.status === "pending" && (
                      <>
                        <div className="field">
                          <label className="field-label" htmlFor="review-patch">
                            {t.correctionPatch}
                          </label>
                          <textarea
                            id="review-patch"
                            aria-label={t.correctionPatch}
                            rows={6}
                            value={patch}
                            onChange={(event) => setPatch(event.target.value)}
                          />
                          <p className="field-hint">{t.correctionPatchHint}</p>
                        </div>
                        <div className="save-bar">
                          <button
                            type="button"
                            className="primary"
                            onClick={() => void review("approve")}
                          >
                            {t.correctionApprove}
                          </button>
                          <button
                            type="button"
                            onClick={() => void review("approve", true)}
                          >
                            {t.correctionApproveEdited}
                          </button>
                          <button
                            type="button"
                            className="danger"
                            onClick={() => void review("reject")}
                          >
                            {t.correctionReject}
                          </button>
                        </div>
                      </>
                    )}
                  </>
                ) : (
                  <div className="empty-state">{t.noSelection}</div>
                )}
              </section>
            </div>
          ) : (
            <section className="card section-card">
              <div className="card-title-row">
                <h2 className="card-title">{t.evalQuestionsTab}</h2>
                <Space wrap>
                  <Select
                    aria-label="按划分筛选"
                    value={evalSplitFilter}
                    onChange={setEvalSplitFilter}
                    style={{ minWidth: 130 }}
                    options={[
                      { value: "all", label: t.filterAll },
                      { value: "dev", label: t.evalSplitDev },
                      { value: "holdout", label: t.evalSplitHoldout },
                    ]}
                  />
                  <Select
                    aria-label="按确认状态筛选"
                    value={evalConfirmedFilter}
                    onChange={setEvalConfirmedFilter}
                    style={{ minWidth: 130 }}
                    options={[
                      { value: "all", label: t.filterAll },
                      { value: "confirmed", label: t.evalConfirmed },
                      { value: "unconfirmed", label: t.evalUnconfirmed },
                    ]}
                  />
                  <Button
                    autoInsertSpace={false}
                    type="primary"
                    onClick={openCreateEvalModal}
                  >
                    {t.evalAddQuestion}
                  </Button>
                </Space>
              </div>
              {evalError && (
                <Alert
                  type="error"
                  title={evalError}
                  style={{ marginBottom: 12 }}
                />
              )}
              <TableScroll label={t.evalQuestionsTab}>
                <Table
                  rowKey="id"
                  pagination={false}
                  dataSource={filteredEvalQuestions}
                  locale={{ emptyText: t.emptyContext }}
                  columns={[
                    { title: t.evalQuestionLabel, dataIndex: "question" },
                    { title: t.evalCategoryLabel, dataIndex: "category" },
                    {
                      title: t.evalSplitLabel,
                      render: (_, row) => (
                        <Tag
                          color={row.split === "holdout" ? "purple" : "blue"}
                        >
                          {row.split}
                        </Tag>
                      ),
                    },
                    {
                      title: t.buildStatus,
                      render: (_, row) => (
                        <Space size={4}>
                          <Tag
                            color={row.answer_confirmed ? "green" : "orange"}
                          >
                            {row.answer_confirmed
                              ? t.evalConfirmed
                              : t.evalUnconfirmed}
                          </Tag>
                          {row.redacted && <Tag>{t.evalRedacted}</Tag>}
                        </Space>
                      ),
                    },
                    {
                      title: t.evalSqlLabel,
                      render: (_, row) =>
                        row.reference_sql ? (
                          <code className="mono">{row.reference_sql}</code>
                        ) : (
                          "—"
                        ),
                    },
                    {
                      title: t.actions,
                      render: (_, row) => (
                        <Space size={6}>
                          {!row.answer_confirmed && (
                            <Button
                              size="small"
                              type="primary"
                              autoInsertSpace={false}
                              onClick={() => void confirmEvalQuestion(row)}
                            >
                              {t.evalConfirm}
                            </Button>
                          )}
                          {!row.redacted && (
                            <Button
                              size="small"
                              autoInsertSpace={false}
                              onClick={() => openEditEvalModal(row)}
                            >
                              {t.evalEditQuestion}
                            </Button>
                          )}
                          <Button
                            size="small"
                            danger
                            autoInsertSpace={false}
                            onClick={() => void deleteEvalQuestion(row)}
                          >
                            {t.deleteToken}
                          </Button>
                        </Space>
                      ),
                    },
                  ]}
                />
              </TableScroll>
              <Modal
                open={evalModalOpen}
                title={editingEval ? t.evalEditQuestion : t.evalAddQuestion}
                okText={t.evalSaveQuestion}
                okButtonProps={{ autoInsertSpace: false }}
                cancelText={t.cancel}
                confirmLoading={eqSaving}
                onOk={() => void saveEvalQuestion()}
                onCancel={() => setEvalModalOpen(false)}
              >
                <div className="stack">
                  <div className="field">
                    <label className="field-label" htmlFor="eval-q-input">
                      {t.evalQuestionLabel}
                    </label>
                    <Input
                      id="eval-q-input"
                      aria-label={t.evalQuestionLabel}
                      value={eqQuestion}
                      onChange={(e) => setEqQuestion(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="eval-caliber-input">
                      {t.evalCaliberLabel}
                    </label>
                    <Input
                      id="eval-caliber-input"
                      aria-label={t.evalCaliberLabel}
                      value={eqCaliber}
                      onChange={(e) => setEqCaliber(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="eval-sql-input">
                      {t.evalSqlLabel}
                    </label>
                    <Input.TextArea
                      id="eval-sql-input"
                      aria-label={t.evalSqlLabel}
                      rows={3}
                      value={eqSql}
                      onChange={(e) => setEqSql(e.target.value)}
                    />
                  </div>
                  <Space wrap>
                    <div className="field">
                      <label className="field-label" htmlFor="eval-cat-input">
                        {t.evalCategoryLabel}
                      </label>
                      <Input
                        id="eval-cat-input"
                        aria-label={t.evalCategoryLabel}
                        value={eqCategory}
                        onChange={(e) => setEqCategory(e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <span className="field-label">{t.evalDialectLabel}</span>
                      <Select
                        aria-label={t.evalDialectLabel}
                        value={eqDialect}
                        onChange={setEqDialect}
                        style={{ minWidth: 120 }}
                        options={[
                          { value: "hive", label: "hive" },
                          { value: "spark", label: "spark" },
                          { value: "doris", label: "doris" },
                        ]}
                      />
                    </div>
                    <div className="field">
                      <span className="field-label">{t.evalSplitLabel}</span>
                      <Select
                        aria-label={t.evalSplitLabel}
                        value={eqSplit}
                        onChange={setEqSplit}
                        style={{ minWidth: 130 }}
                        options={[
                          { value: "dev", label: t.evalSplitDev },
                          { value: "holdout", label: t.evalSplitHoldout },
                        ]}
                      />
                    </div>
                  </Space>
                  <Checkbox
                    checked={eqConfirmed}
                    onChange={(e) => setEqConfirmed(e.target.checked)}
                  >
                    {t.evalConfirmed}
                  </Checkbox>
                </div>
              </Modal>
            </section>
          )}
        </>
      )}
      {draftGap && draftGap.project_id === project && (
        <CorrectionDraftDrawer
          open
          token={token}
          project={draftGap.project_id}
          messages={t}
          knowledgeGapId={draftGap.id}
          initialDescription={gapDescription(
            draftGap.question,
            draftGap.missing,
          )}
          onClose={() => setDraftGap(null)}
        />
      )}
    </section>
  );
}
