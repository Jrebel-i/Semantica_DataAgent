import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Select, Space, Tag } from "antd";
import { readErrorDetail } from "./httpErrors";
import { DiffViewer } from "./DiffViewer";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import { ProjectSelector, storedProject } from "./ProjectSelector";
import { formatOverlapRatio, formatProbePartitions } from "./DiscoveryPanel";
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

type ContextExcerpt = {
  sequence?: number;
  question?: string;
  is_target?: boolean;
};

type TriggerEvidence = {
  question?: string;
  answer?: string;
  description?: string;
  conversation_id?: string | null;
  turn_id?: number | null;
  turn_count?: number;
  synthesis_mode?: string;
  reference_sql?: string;
  dialect?: string;
  destination_kind?: string;
  context_excerpts?: ContextExcerpt[];
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

type DestinationKind =
  | "historical_ticket"
  | "knowledge_doc"
  | "semantic_override"
  | "ossie_model";

type Proposal = {
  id: string;
  target: string;
  reason: string;
  diff: string;
  patch: string;
  status: string;
  source?: string;
  target_kind?: string;
  destination_kind?: DestinationKind;
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
type SourceFilter =
  | "all"
  | "manual"
  | "feedback"
  | "mcp"
  | "evolution"
  | "discovery";
export type DestinationFilter = "all" | DestinationKind;

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
  if (source === "feedback") return t.sourceFeedback;
  if (source === "mcp") return t.sourceMcp;
  if (source === "evolution") return t.sourceEvolution;
  if (source === "discovery") return t.sourceDiscovery;
  if (source === "manual") return t.sourceManual;
  return source || "";
}

function resolveDestinationKind(row: Proposal): DestinationKind {
  if (row.destination_kind) return row.destination_kind;
  if (row.target_kind === "override" || row.structured_patch) {
    return "semantic_override";
  }
  if (row.target_kind === "ossie") return "ossie_model";
  const decoded = safeDecodePath(row.target);
  if (
    decoded.includes("/historical-tickets/") ||
    decoded.startsWith("document:historical-tickets/")
  ) {
    return "historical_ticket";
  }
  return "knowledge_doc";
}

function destinationMeta(
  row: Proposal,
  t: Messages,
): { kind: DestinationKind; label: string; color: string } {
  const kind = resolveDestinationKind(row);
  if (kind === "historical_ticket") {
    return { kind, label: t.destHistoricalTicket, color: "geekblue" };
  }
  if (kind === "semantic_override") {
    return { kind, label: t.destSemanticOverride, color: "cyan" };
  }
  if (kind === "ossie_model") {
    return { kind, label: t.destOssieModel, color: "orange" };
  }
  return { kind, label: t.destKnowledgeDoc, color: "purple" };
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
  if (kind === "historical_ticket") return t.discoveryKindHistoricalTicket;
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

      {(trigger.conversation_id ||
        trigger.knowledge_gap_id ||
        trigger.synthesis_mode ||
        typeof trigger.turn_count === "number") && (
        <div className="discovery-evidence-pills">
          {trigger.conversation_id && (
            <Tag className="mono">
              {t.evidenceConversation}: {trigger.conversation_id}
              {typeof trigger.turn_id === "number" ? `#${trigger.turn_id}` : ""}
            </Tag>
          )}
          {typeof trigger.turn_count === "number" && trigger.turn_count > 0 && (
            <Tag>上下文轮次: {trigger.turn_count}</Tag>
          )}
          {trigger.synthesis_mode && (
            <Tag color={trigger.synthesis_mode === "llm" ? "blue" : "default"}>
              {trigger.synthesis_mode === "llm"
                ? t.synthesisModeLlm
                : t.synthesisModeFallback}
            </Tag>
          )}
          {trigger.knowledge_gap_id && (
            <Tag className="mono">
              {t.evidenceKnowledgeGap}: {trigger.knowledge_gap_id}
            </Tag>
          )}
        </div>
      )}
      {Array.isArray(trigger.context_excerpts) &&
        trigger.context_excerpts.length > 0 && (
          <div className="evidence-excerpt-list">
            {trigger.context_excerpts.map((item, idx) => (
              <div key={idx} className="evidence-excerpt-item">
                <strong className="mono">
                  #{item.sequence ?? idx + 1}
                  {item.is_target ? " (目标轮次)" : ""}:{" "}
                </strong>
                <span>{item.question}</span>
              </div>
            ))}
          </div>
        )}
      {trigger.reference_sql && (
        <pre className="source-raw mono">{trigger.reference_sql}</pre>
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
  initialDestinationFilter,
  onNavigateGaps,
}: {
  token: string;
  gaps?: boolean;
  messages: Messages;
  initialProposalId?: string | null;
  initialDestinationFilter?: DestinationFilter | null;
  onNavigateGaps?: () => void;
}) {
  const [project, setProject] = useState(storedProject);
  const [rows, setRows] = useState<Proposal[]>([]);
  const [missing, setMissing] = useState<Gap[]>([]);
  const [missingProject, setMissingProject] = useState("");
  const [gapStatusFilter, setGapStatusFilter] = useState<
    "all" | "pending" | "confirmed" | "rejected"
  >("all");
  const [gapSearch, setGapSearch] = useState("");
  const [selectedGapId, setSelectedGapId] = useState("");
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
  const urlDestination = new URLSearchParams(window.location.search).get(
    "destination",
  );
  const initialDest: DestinationFilter =
    initialDestinationFilter ??
    (urlDestination === "historical_ticket" ||
    urlDestination === "knowledge_doc" ||
    urlDestination === "semantic_override" ||
    urlDestination === "ossie_model"
      ? urlDestination
      : "all");
  useEffect(() => {
    if (!targetProposalId || rows.length === 0) return;
    const match = rows.find((row) => row.id === targetProposalId);
    if (match) {
      setSelected(match);
      setPatch(initialPatchForProposal(match));
    }
  }, [rows, targetProposalId]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [destinationFilter, setDestinationFilter] =
    useState<DestinationFilter>(initialDest);
  useEffect(() => {
    if (initialDestinationFilter) {
      setDestinationFilter(initialDestinationFilter);
    }
  }, [initialDestinationFilter]);
  const [patch, setPatch] = useState("");
  const [error, setError] = useState("");
  const [reverting, setReverting] = useState(false);

  const projectQuery = `project=${encodeURIComponent(project)}`;

  const sourceScopedRows = rows.filter((row) => {
    if (sourceFilter !== "all" && (row.source || "manual") !== sourceFilter) {
      return false;
    }
    if (
      destinationFilter !== "all" &&
      resolveDestinationKind(row) !== destinationFilter
    ) {
      return false;
    }
    return true;
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

  const totalPendingProposals = rows.filter(
    (row) => row.status === "pending",
  ).length;
  const totalApprovedProposals = rows.filter(
    (row) => row.status === "approved" || row.status === "auto_applied",
  ).length;
  const totalRejectedProposals = rows.filter(
    (row) => row.status === "rejected",
  ).length;

  const projectGaps = missingProject === project ? missing : [];
  const gapPendingCount = projectGaps.filter(
    (gap) => gap.status === "pending",
  ).length;
  const gapConfirmedCount = projectGaps.filter(
    (gap) => gap.status === "confirmed",
  ).length;
  const gapRejectedCount = projectGaps.filter(
    (gap) => gap.status === "rejected",
  ).length;
  const trimmedGapQuery = gapSearch.trim().toLowerCase();
  const filteredGaps = projectGaps.filter((gap) => {
    if (gapStatusFilter !== "all" && gap.status !== gapStatusFilter) {
      return false;
    }
    if (trimmedGapQuery) {
      return (
        gap.question.toLowerCase().includes(trimmedGapQuery) ||
        (gap.missing || "").toLowerCase().includes(trimmedGapQuery)
      );
    }
    return true;
  });
  const selectedGap =
    filteredGaps.find((gap) => gap.id === selectedGapId) ??
    filteredGaps[0] ??
    null;

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

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    setSelected(null);
    setSelectedGapId("");
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

  const canRevert =
    selected?.status === "approved" || selected?.status === "auto_applied";
  const selectedGapStatusText = selectedGap
    ? selectedGap.status === "confirmed"
      ? t.gapConfirmed
      : selectedGap.status === "rejected"
        ? t.gapReject
        : t.statusPending
    : "";
  const selectedGapStatusColor = selectedGap
    ? selectedGap.status === "confirmed"
      ? "green"
      : selectedGap.status === "rejected"
        ? "red"
        : "gold"
    : "default";
  const selectedGapIdFromProposal =
    selected?.evidence?.trigger_evidence?.knowledge_gap_id ??
    selected?.gate_report?.evidence?.trigger_evidence?.knowledge_gap_id;

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {gaps ? t.gapTitle : t.correctionTitle}
          </h1>
          <p className="page-subtitle">{gaps ? t.gapHint : t.correctionHint}</p>
        </div>
        <button type="button" onClick={() => void refresh()}>
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
        <>
          <section className="card gov-metrics-card">
            <div className="gov-metrics">
              <div className="gov-metric">
                <span className="gov-metric-label">{t.gapMetricPending}</span>
                <strong>{gapPendingCount}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">{t.gapMetricConfirmed}</span>
                <strong>{gapConfirmedCount}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">{t.gapMetricRejected}</span>
                <strong>{gapRejectedCount}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">{t.gapMetricTotal}</span>
                <strong>{projectGaps.length}</strong>
              </div>
            </div>
          </section>
          <div className="gov-split-layout">
            <section className="card section-card">
              <div className="card-title-row">
                <h2 className="card-title">{t.gapListTitle}</h2>
                <Select
                  aria-label={t.gapFilterAll}
                  value={gapStatusFilter}
                  onChange={setGapStatusFilter}
                  style={{ minWidth: 120 }}
                  options={[
                    { value: "all", label: t.gapFilterAll },
                    { value: "pending", label: t.gapMetricPending },
                    { value: "confirmed", label: t.gapMetricConfirmed },
                    { value: "rejected", label: t.gapMetricRejected },
                  ]}
                />
              </div>
              <Input
                className="gov-gap-search"
                aria-label={t.gapSearchPlaceholder}
                placeholder={t.gapSearchPlaceholder}
                value={gapSearch}
                onChange={(event) => setGapSearch(event.target.value)}
              />
              {filteredGaps.length === 0 ? (
                <div className="empty-state">{t.emptyContext}</div>
              ) : (
                <ul className="correction-list">
                  {filteredGaps.map((gap) => {
                    const statusText =
                      gap.status === "confirmed"
                        ? t.gapConfirmed
                        : gap.status === "rejected"
                          ? t.gapReject
                          : t.statusPending;
                    const createdText = gap.created_at
                      ? new Date(gap.created_at).toLocaleString()
                      : "";
                    return (
                      <li key={gap.id}>
                        <button
                          type="button"
                          className={selectedGap?.id === gap.id ? "active" : ""}
                          onClick={() => setSelectedGapId(gap.id)}
                        >
                          <div>
                            <strong>{gap.question}</strong>
                            <span className="gov-list-item-meta">
                              {createdText
                                ? `${statusText} · ${createdText}`
                                : statusText}
                            </span>
                          </div>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
            <section className="card section-card">
              {selectedGap ? (
                <>
                  <div className="card-title-row">
                    <h2 className="card-title">{t.gapDetailTitle}</h2>
                    <Tag color={selectedGapStatusColor}>
                      {selectedGapStatusText}
                    </Tag>
                  </div>
                  <h3 className="gov-detail-question">
                    {selectedGap.question}
                  </h3>
                  <p className="gov-detail-missing">{selectedGap.missing}</p>
                  <div className="gov-info-banner">
                    <div className="stack" style={{ gap: 4 }}>
                      {selectedGap.created_at && (
                        <span>
                          {t.gapCreated}:{" "}
                          {new Date(selectedGap.created_at).toLocaleString()}
                        </span>
                      )}
                      {selectedGap.reviewed_at && (
                        <span>
                          {t.gapReviewed}:{" "}
                          {new Date(selectedGap.reviewed_at).toLocaleString()}{" "}
                          {selectedGap.reviewer_name}
                        </span>
                      )}
                    </div>
                  </div>
                  {selectedGap.status === "pending" &&
                    selectedGap.can_review && (
                      <Space wrap>
                        <Button
                          type="primary"
                          onClick={() =>
                            void reviewGap(selectedGap, "confirmed")
                          }
                        >
                          {t.gapConfirm}
                        </Button>
                        <Button
                          onClick={() =>
                            void reviewGap(selectedGap, "rejected")
                          }
                        >
                          {t.gapReject}
                        </Button>
                      </Space>
                    )}
                  {selectedGap.status === "confirmed" && (
                    <Space wrap>
                      <Button
                        type="primary"
                        onClick={() => setDraftGap(selectedGap)}
                      >
                        {t.gapCorrection}
                      </Button>
                      {selectedGap.can_review && (
                        <a
                          href={`/source?project=${encodeURIComponent(selectedGap.project_id)}&gap=${encodeURIComponent(selectedGap.id)}`}
                        >
                          {t.gapAddKnowledge}
                        </a>
                      )}
                    </Space>
                  )}
                </>
              ) : (
                <div className="empty-state">{t.emptyContext}</div>
              )}
            </section>
          </div>
        </>
      ) : (
        <>
          <section className="card gov-metrics-card">
            <div className="gov-metrics">
              <div className="gov-metric">
                <span className="gov-metric-label">
                  {t.correctionMetricPending}
                </span>
                <strong>{totalPendingProposals}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">
                  {t.correctionMetricApproved}
                </span>
                <strong>{totalApprovedProposals}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">
                  {t.correctionMetricRejected}
                </span>
                <strong>{totalRejectedProposals}</strong>
              </div>
              <div className="gov-metric">
                <span className="gov-metric-label">
                  {t.correctionMetricTotal}
                </span>
                <strong>{rows.length}</strong>
              </div>
            </div>
          </section>
          <div className="gov-split-layout">
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
                        ["all", `${t.filterAll} (${sourceScopedRows.length})`],
                        ["pending", `${t.filterPending} (${pendingCount})`],
                        ["resolved", `${t.filterResolved} (${resolvedCount})`],
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
                        ["feedback", t.sourceFeedback],
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
                  <div className="correction-filters" role="group">
                    {(
                      [
                        ["all", t.destFilterAll],
                        ["historical_ticket", t.destHistoricalTicket],
                        ["knowledge_doc", t.destKnowledgeDoc],
                        ["semantic_override", t.destSemanticOverride],
                        ["ossie_model", t.destOssieModel],
                      ] as const
                    ).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        className={destinationFilter === key ? "active" : ""}
                        aria-pressed={destinationFilter === key}
                        onClick={() => setDestinationFilter(key)}
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
                    const srcText = sourceLabel(row.source || "manual", t);
                    const dest = destinationMeta(row, t);
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
                          <Space size={4} wrap>
                            {isNewFileProposal(row) && (
                              <Tag color="purple">{t.draftNewFileBadge}</Tag>
                            )}
                            {srcText && (
                              <Tag>
                                {t.proposalSourcePrefix}: {srcText}
                              </Tag>
                            )}
                            <Tag color={dest.color}>
                              {t.proposalDestPrefix}: {dest.label}
                            </Tag>
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
                    <Space size={6} wrap>
                      {isNewFileProposal(selected) && (
                        <Tag color="purple">{t.draftNewFileBadge}</Tag>
                      )}
                      {selected.source && (
                        <Tag>
                          {t.proposalSourcePrefix}:{" "}
                          {sourceLabel(selected.source, t)}
                        </Tag>
                      )}
                      {(() => {
                        const dest = destinationMeta(selected, t);
                        return (
                          <Tag color={dest.color}>
                            {t.proposalDestPrefix}: {dest.label}
                          </Tag>
                        );
                      })()}
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
                  {selectedGapIdFromProposal && onNavigateGaps && (
                    <div className="gov-info-banner">
                      <span>
                        {t.evidenceKnowledgeGap}: {selectedGapIdFromProposal}
                      </span>
                      <Button type="link" size="small" onClick={onNavigateGaps}>
                        {t.gapViewLink}
                      </Button>
                    </div>
                  )}
                  {selected.gate_report && (
                    <>
                      <Alert
                        style={{ marginBottom: 12 }}
                        type={selected.gate_report.passed ? "success" : "error"}
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
                                {selected.gate_report.provenance.round_number ??
                                  "-"}{" "}
                                ·{" "}
                                {selected.gate_report.provenance.signal_kind ??
                                  "-"}
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
                                {selected.gate_report.l2_gate.evaluated_cases ??
                                  0}
                                {selected.gate_report.l2_gate.mode ===
                                "cold_start_ab"
                                  ? ` · W_c=${selected.gate_report.l2_gate.W_c ?? 0} W_p=${selected.gate_report.l2_gate.W_p ?? 0} E_c=${selected.gate_report.l2_gate.E_c ?? 0}`
                                  : selected.gate_report.l2_gate.regressions !==
                                      undefined
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
