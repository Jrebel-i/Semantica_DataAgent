import { createPortal } from "react-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";
import type { Language, Messages } from "./i18n";
import { PlusOutlined } from "@ant-design/icons";
import { Markdown } from "./Markdown";
import {
  citationLabel,
  citationsFromRefs,
  stripCitationRefs,
} from "./chat/citations";
import type {
  AvailableSkill,
  ConversationDetail,
  LiveTurn,
  Project,
  PublishedAgentView,
  Turn,
} from "./chat/types";
import { copyText } from "./chat/sse";
import { ResultTable } from "./chat/ResultTable";
import { ToolList } from "./chat/ToolList";
import { SqlList } from "./chat/SqlList";
import { CitationList } from "./chat/CitationList";
import { ClarificationList } from "./chat/ClarificationList";
import { QuestionText } from "./chat/QuestionText";
import { TurnBlock } from "./chat/TurnBlock";
import { ConversationSidebar } from "./chat/ConversationSidebar";
import { Composer } from "./chat/Composer";
import { ContextDrawer } from "./chat/ContextDrawer";
import { CorrectionDraftDrawer } from "./chat/CorrectionDraftDrawer";
import { ProviderSettingsPage } from "./chat/ProviderSettings";
import { isPublishedPair, useProviders } from "./chat/useProviders";
import { useConversations } from "./chat/useConversations";
import {
  PROJECT_CHANGE_EVENT,
  rememberProject,
  storedProject,
} from "./ProjectSelector";
import { useCitations } from "./chat/useCitations";
import { useAsk } from "./chat/useAsk";
import { turnFailed } from "./chat/turnEvents";
import { readErrorDetail } from "./httpErrors";

const CONVERSATION_LOAD_TIMEOUT_MS = 30_000;

export {
  citationDisplayName,
  citationLabel,
  mergeCitations,
  stripCitationRefs,
} from "./chat/citations";
export type { CitationGroup, CitationLabel } from "./chat/types";

export function ChatPage({
  token,
  role,
  messages: t,
  settingsOnly = false,
  sessionTarget,
  newTarget,
  initialConversationId = "",
  onNavigateChat,
  onNavigateKnowledge,
  onNavigatePersonalSettings,
  refreshKey = 0,
  active = true,
}: {
  token: string;
  role: string;
  messages: Messages;
  settingsOnly?: boolean;
  sessionTarget?: HTMLElement | null;
  newTarget?: HTMLElement | null;
  initialConversationId?: string;
  onNavigateChat?: () => void;
  onNavigateKnowledge?: () => void;
  onNavigatePersonalSettings?: () => void;
  language?: Language;
  onChangeLanguage?: () => void;
  refreshKey?: number;
  active?: boolean;
}) {
  const askSequence = useRef(0);
  const deepLinkLoaded = useRef("");
  const deepLinkTarget = useRef(initialConversationId);
  const conversationLoad = useRef({ sequence: 0, id: "" });
  const conversationAbort = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      conversationAbort.current?.abort();
    },
    [],
  );
  const [boundConversation, setBoundConversation] =
    useState<ConversationDetail | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [expiredPlans, setExpiredPlans] = useState<string[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(storedProject);
  const [projectFilter, setProjectFilter] = useState("");
  const [resumable, setResumable] = useState(true);
  const [sending, setSending] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [error, setError] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState("");
  const [markingTurnId, setMarkingTurnId] = useState<number | null>(null);
  const [correctionTurn, setCorrectionTurn] = useState<Turn | null>(null);
  const [availableSkills, setAvailableSkills] = useState<AvailableSkill[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<AvailableSkill | null>(
    null,
  );

  useEffect(() => {
    let active = true;
    void fetch("/api/v1/projects", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((response) => (response.ok ? response.json() : []))
      .then((rows: Project[]) => {
        if (active && Array.isArray(rows)) setProjects(rows);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [token]);

  useEffect(() => {
    if (!projects.length) return;
    const current = projects.find((project) => project.id === projectId);
    if (current?.status === "active") return;
    if (currentConversationId && current?.status === "archived") return;
    const fallback =
      projects.find(
        (project) => project.id === "default" && project.status === "active",
      ) ?? projects.find((project) => project.status === "active");
    if (!fallback) return;
    setProjectId(fallback.id);
    rememberProject(fallback.id);
  }, [projects, projectId, currentConversationId]);

  function selectProject(value: string) {
    setProjectId(value);
    rememberProject(value);
  }

  useEffect(() => {
    const onProjectChange = (event: Event) => {
      if (currentConversationId || live) return;
      const next = (event as CustomEvent<string>).detail;
      if (!next || next === projectId) return;
      if (
        projects.length > 0 &&
        !projects.some((p) => p.id === next && p.status === "active")
      ) {
        return;
      }
      setProjectId(next);
    };
    window.addEventListener(PROJECT_CHANGE_EVENT, onProjectChange);
    return () => {
      window.removeEventListener(PROJECT_CHANGE_EVENT, onProjectChange);
    };
  }, [currentConversationId, live, projectId, projects]);

  const {
    conversations,
    archivedConversations,
    showArchived,
    menuId,
    editingId,
    draftTitle,
    hoveredConversationId,
    focusedConversationId,
    confirmingDeleteId,
    deletingConversationId,
    sessionError,
    setShowArchived,
    setMenuId,
    setEditingId,
    setDraftTitle,
    setHoveredConversationId,
    setFocusedConversationId,
    setConfirmingDeleteId,
    refreshConversations,
    deleteConversation,
    patchConversation,
    saveTitle,
  } = useConversations({
    token,
    messages: t,
    settingsOnly,
    refreshKey,
    currentConversationId,
    onNewConversation: newConversation,
    onError: setError,
  });

  const {
    catalogMode,
    catalogLoaded,
    catalogAgents,
    personalDefaults,
    activeCatalogAgent,
    noPublishedAgents,
    missingDefaultPrompt,
    personalDefaultNotice,
    personalDefaultError,
    savingPersonalDefault,
    agents,
    profiles,
    providers,
    agentId,
    providerId,
    model,
    effort,
    effortLevels,
    allowedEfforts,
    selectionTouched,
    activeProvider,
    setAgentId,
    setProviderId,
    setModel,
    selectAgent,
    selectProvider,
    selectModel,
    selectEffort,
    resetToNewConversationDefaults,
    savePersonalDefault,
    resetPersonalDefault,
  } = useProviders({
    token,
    messages: t,
    onError: setError,
    refreshKey,
    active,
    hasBoundConversation: Boolean(currentConversationId),
  });

  const visionEnabled =
    activeProvider?.model_capabilities?.[model]?.vision ?? false;

  const {
    copied,
    citationRef,
    citationDetail,
    citationError,
    citationLoading,
    resetCitation,
    openCitation,
    copySql,
    closeCitation,
  } = useCitations({ token, messages: t, setError });

  const selectedProject = projects.find((project) => project.id === projectId);
  const projectArchived = selectedProject?.status === "archived";

  useEffect(() => {
    if (settingsOnly || !token || projectArchived) {
      setAvailableSkills([]);
      setSelectedSkill(null);
      return;
    }
    let active = true;
    const query = projectId ? `?project=${encodeURIComponent(projectId)}` : "";
    void fetch(`/api/v1/skills/available${query}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((response) => (response.ok ? response.json() : []))
      .then((rows: AvailableSkill[]) => {
        if (!active) return;
        const list = Array.isArray(rows) ? rows : [];
        setAvailableSkills(list);
        setSelectedSkill((current) => {
          if (!current) return null;
          return list.find((item) => item.name === current.name) ?? null;
        });
      })
      .catch(() => {
        if (active) setAvailableSkills([]);
      });
    return () => {
      active = false;
    };
  }, [token, projectId, refreshKey, settingsOnly, projectArchived]);

  const currentAgent = agents.find((item) => item.id === agentId);
  const agentSupportsSkills = currentAgent?.supports_skills ?? true;

  async function markTurnAccurate(turn: Turn) {
    if (!currentConversationId || projectArchived) return;
    setMarkingTurnId(turn.id);
    setError("");
    setFeedbackNotice("");
    const url = `/api/v1/conversations/${encodeURIComponent(currentConversationId)}/turns/${turn.id}/feedback`;
    try {
      if (turn.feedback_accurate) {
        const response = await fetch(url, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) {
          throw new Error(await readErrorDetail(response, t.operationFailed));
        }
        setTurns((prev) =>
          prev.map((row) =>
            row.id === turn.id
              ? {
                  ...row,
                  feedback_accurate: false,
                  eval_question_id: null,
                  eval_answer_confirmed: null,
                }
              : row,
          ),
        );
        setFeedbackNotice(t.turnAccurateCancelled);
        return;
      }
      const response = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ rating: "accurate" }),
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.operationFailed));
      }
      const saved = (await response.json()) as {
        question_id: string;
        answer_confirmed: boolean;
      };
      setTurns((prev) =>
        prev.map((row) =>
          row.id === turn.id
            ? {
                ...row,
                feedback_accurate: true,
                eval_question_id: saved.question_id,
                eval_answer_confirmed: saved.answer_confirmed,
              }
            : row,
        ),
      );
      setFeedbackNotice(
        saved.answer_confirmed
          ? t.turnAccurateSavedConfirmed
          : t.turnAccurateSavedDraft,
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.operationFailed);
    } finally {
      setMarkingTurnId(null);
    }
  }

  const boundProfile =
    catalogMode && currentConversationId && boundConversation
      ? boundConversation.agent_profile_id
        ? catalogAgents.find((a) => a.id === boundConversation.agent_profile_id)
        : (catalogAgents.find((a) => a.id === boundConversation.agent) ??
          catalogAgents.find((a) => a.runner_id === boundConversation.agent))
      : undefined;
  const conversationAgentDisabled = Boolean(
    catalogMode &&
    catalogLoaded &&
    currentConversationId &&
    boundConversation &&
    !boundProfile,
  );
  const conversationModelInvalid = Boolean(
    catalogMode &&
    catalogLoaded &&
    currentConversationId &&
    boundConversation &&
    boundProfile &&
    !isPublishedPair(boundProfile, providerId, model),
  );

  const {
    question,
    setQuestion,
    ask,
    chooseClarification,
    canAsk,
    executePlan,
    abortAsk,
    attachments,
    attachmentError,
    addAttachments,
    removeAttachment,
    clearAttachments,
  } = useAsk({
    token,
    messages: t,
    agentId: catalogMode
      ? (activeCatalogAgent?.runner_id ?? boundConversation?.agent ?? agentId)
      : agentId,
    agentProfileId: catalogMode ? activeCatalogAgent?.id : undefined,
    providerId,
    model,
    effort,
    projectId,
    visionEnabled,
    projectArchived,
    blockedByConversationState:
      noPublishedAgents ||
      conversationAgentDisabled ||
      conversationModelInvalid,
    currentConversationId,
    setCurrentConversationId,
    sending,
    setSending,
    resumable,
    archivedConversations,
    selectionTouched,
    askSequence,
    resetCitation,
    setLive,
    setTurns,
    setError,
    setExpiredPlans,
    refreshConversations,
    openConversation,
    loadCompletedConversation,
    selectedSkill,
    clearSelectedSkill: () => setSelectedSkill(null),
    agentSupportsSkills,
  });

  async function downloadLinks(id: string): Promise<Record<string, string>> {
    const response = await fetch(
      `/api/v1/queries/${encodeURIComponent(id)}/download-links`,
      { method: "POST", headers: { Authorization: `Bearer ${token}` } },
    );
    if (!response.ok) throw new Error(t.askError);
    const paths = (await response.json()) as Record<string, string>;
    return Object.fromEntries(
      Object.entries(paths).map(([format, path]) => [
        format,
        new URL(path, window.location.origin).href,
      ]),
    );
  }
  async function downloadQuery(id: string, format: string) {
    try {
      const links = await downloadLinks(id);
      const anchor = document.createElement("a");
      anchor.href = links[format];
      anchor.rel = "noreferrer";
      anchor.click();
    } catch {
      setError(t.askError);
    }
  }
  async function copyDownloadLink(id: string) {
    try {
      const links = await downloadLinks(id);
      await copyText(links.csv);
    } catch {
      setError(t.copyError);
    }
  }

  async function openConversation(id: string, fromDeepLink = false) {
    abortAsk();
    conversationAbort.current?.abort();
    const controller = new AbortController();
    conversationAbort.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, CONVERSATION_LOAD_TIMEOUT_MS);
    const sequence = conversationLoad.current.sequence + 1;
    conversationLoad.current = { sequence, id };
    setLoadingConversation(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/conversations/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error();
      const detail = (await response.json()) as ConversationDetail;
      if (conversationLoad.current.sequence !== sequence) return;
      if (fromDeepLink && deepLinkTarget.current !== id) return;
      applyConversation(detail);
    } catch {
      if (conversationLoad.current.sequence !== sequence) return;
      if (timedOut) {
        setError(t.conversationLoadTimeout);
      } else if (!controller.signal.aborted) {
        setError(t.askError);
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (conversationAbort.current === controller) {
        conversationAbort.current = null;
      }
      if (conversationLoad.current.sequence === sequence) {
        setLoadingConversation(false);
      }
    }
  }

  const reconciledConversationRef = useRef("");

  const reconcileBoundConversation = useCallback(
    (detail: ConversationDetail, agentsList: PublishedAgentView[]) => {
      const matched = detail.agent_profile_id
        ? agentsList.find((a) => a.id === detail.agent_profile_id)
        : (agentsList.find((a) => a.id === detail.agent) ??
          agentsList.find((a) => a.runner_id === detail.agent));
      if (!matched) {
        setAgentId(detail.agent_profile_id || detail.agent);
        setProviderId(detail.provider_id);
        setModel(detail.model);
        return;
      }
      setAgentId(matched.id);
      if (isPublishedPair(matched, detail.provider_id, detail.model)) {
        setProviderId(detail.provider_id);
        setModel(detail.model);
      } else {
        setProviderId(matched.providers[0]?.id ?? "");
        setModel("");
      }
    },
    [setAgentId, setProviderId, setModel],
  );

  useEffect(() => {
    if (!catalogMode || !catalogLoaded || !boundConversation) return;
    if (reconciledConversationRef.current === boundConversation.id) return;
    reconciledConversationRef.current = boundConversation.id;
    reconcileBoundConversation(boundConversation, catalogAgents);
  }, [
    catalogMode,
    catalogLoaded,
    catalogAgents,
    boundConversation,
    reconcileBoundConversation,
  ]);

  function applyConversation(detail: ConversationDetail) {
    selectionTouched.current = true;
    if (detail.project_id) {
      setProjectId(detail.project_id);
      rememberProject(detail.project_id);
    }
    setBoundConversation(detail);
    setCurrentConversationId(detail.id);
    setResumable(detail.resumable !== false);
    setTurns(detail.turns);
    resetCitation();
    setLive(null);

    if (catalogMode && catalogLoaded) {
      reconciledConversationRef.current = detail.id;
      reconcileBoundConversation(detail, catalogAgents);
      return;
    }

    reconciledConversationRef.current = "";
    setAgentId(detail.agent);
    setProviderId(detail.provider_id);
    setModel(detail.model);
  }

  async function loadCompletedConversation(id: string, sequence: number) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        const response = await fetch(`/api/v1/conversations/${id}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) return;
        const detail = (await response.json()) as ConversationDetail;
        if (sequence !== askSequence.current) return;
        if (deepLinkTarget.current && deepLinkTarget.current !== id) return;
        if (conversationLoad.current.id && conversationLoad.current.id !== id) {
          return;
        }
        const last = detail.turns.at(-1);
        if (last && last.status !== "running") {
          applyConversation(detail);
          return;
        }
      } catch {
        return;
      }
      await new Promise((resolve) =>
        window.setTimeout(resolve, 100 * (attempt + 1)),
      );
    }
  }

  useEffect(() => {
    deepLinkTarget.current = initialConversationId;
    if (!initialConversationId) {
      deepLinkLoaded.current = "";
      return;
    }
    if (!token) return;
    if (initialConversationId === currentConversationId) {
      deepLinkLoaded.current = initialConversationId;
      if (conversationLoad.current.id !== initialConversationId) {
        conversationAbort.current?.abort();
        conversationAbort.current = null;
        conversationLoad.current = {
          sequence: conversationLoad.current.sequence + 1,
          id: "",
        };
        setLoadingConversation(false);
      }
      return;
    }
    if (deepLinkLoaded.current === initialConversationId) return;
    deepLinkLoaded.current = initialConversationId;
    void openConversation(initialConversationId, true);
  }, [initialConversationId, token, currentConversationId]);

  function newConversation() {
    abortAsk();
    conversationAbort.current?.abort();
    conversationAbort.current = null;
    conversationLoad.current = {
      sequence: conversationLoad.current.sequence + 1,
      id: "",
    };
    setLoadingConversation(false);
    reconciledConversationRef.current = "";
    setBoundConversation(null);
    setCurrentConversationId("");
    resetToNewConversationDefaults();
    setResumable(true);
    setTurns([]);
    resetCitation();
    setLive(null);
    setError("");
    clearAttachments();
    setSelectedSkill(null);
    if (projectArchived) {
      const fallback =
        projects.find(
          (project) => project.id === "default" && project.status === "active",
        ) ?? projects.find((project) => project.status === "active");
      if (fallback) {
        setProjectId(fallback.id);
        rememberProject(fallback.id);
      }
    }
  }

  const currentConversation = [...conversations, ...archivedConversations].find(
    (conversation) => conversation.id === currentConversationId,
  );
  const conversationSelectionChanged = Boolean(
    currentConversationId &&
    currentConversation &&
    (providerId !== currentConversation.provider_id ||
      model !== currentConversation.model),
  );
  const selectedTable =
    citationDetail?.kind === "table" ? citationDetail.table : null;
  const citationHeading = citationRef
    ? citationLabel(citationDetail?.ref ?? citationRef)
    : null;
  const emptyConversation = !loadingConversation && turns.length === 0 && !live;
  const projectLocked = Boolean(currentConversationId) || Boolean(live);
  const activeChatProject = projects.find(
    (project) => project.id === (currentConversation?.project_id || projectId),
  );
  const headerProjectName =
    currentConversation?.project_name || activeChatProject?.name || "";
  const showHeaderProjectBadge =
    Boolean(headerProjectName) &&
    (projects.length > 1 || Boolean(currentConversation?.project_id));
  const liveAnswer = live
    ? stripCitationRefs(live.text)
    : { text: "", refs: [] };
  const liveCitations = live
    ? [...live.citations, ...citationsFromRefs(liveAnswer.refs)]
    : [];

  return (
    <section style={{ height: "100%", minHeight: 0 }}>
      {!settingsOnly && (
        <div
          className={`chat-layout${!citationRef ? " context-hidden" : ""}${emptyConversation ? " chat-layout--empty" : ""}`}
        >
          {newTarget &&
            createPortal(
              <Button
                type="text"
                className="nav-item new-conversation"
                icon={<PlusOutlined />}
                aria-label={t.newConversation}
                title={t.newConversation}
                onClick={() => {
                  newConversation();
                  onNavigateChat?.();
                }}
              >
                {t.newConversation}
              </Button>,
              newTarget,
            )}
          {sessionTarget ? (
            createPortal(
              <ConversationSidebar
                messages={t}
                conversations={conversations}
                archivedConversations={archivedConversations}
                projects={projects}
                projectFilter={projectFilter}
                onProjectFilterChange={setProjectFilter}
                showArchived={showArchived}
                currentConversationId={currentConversationId}
                sessionTarget={sessionTarget}
                sessionError={sessionError}
                sending={sending}
                menuId={menuId}
                setMenuId={setMenuId}
                editingId={editingId}
                setEditingId={setEditingId}
                draftTitle={draftTitle}
                setDraftTitle={setDraftTitle}
                hoveredConversationId={hoveredConversationId}
                setHoveredConversationId={setHoveredConversationId}
                focusedConversationId={focusedConversationId}
                setFocusedConversationId={setFocusedConversationId}
                confirmingDeleteId={confirmingDeleteId}
                setConfirmingDeleteId={setConfirmingDeleteId}
                deletingConversationId={deletingConversationId}
                onNewConversation={newConversation}
                onOpenConversation={(id) => {
                  onNavigateChat?.();
                  if (id === currentConversationId && sending) return;
                  void openConversation(id);
                }}
                onPatchConversation={(id, patch) =>
                  void patchConversation(id, patch)
                }
                onSaveTitle={saveTitle}
                onDeleteConversation={(id) => void deleteConversation(id)}
                onToggleArchived={() => setShowArchived(!showArchived)}
              />,
              sessionTarget,
            )
          ) : (
            <ConversationSidebar
              messages={t}
              conversations={conversations}
              archivedConversations={archivedConversations}
              projects={projects}
              projectFilter={projectFilter}
              onProjectFilterChange={setProjectFilter}
              showArchived={showArchived}
              currentConversationId={currentConversationId}
              sessionTarget={sessionTarget}
              sessionError={sessionError}
              sending={sending}
              menuId={menuId}
              setMenuId={setMenuId}
              editingId={editingId}
              setEditingId={setEditingId}
              draftTitle={draftTitle}
              setDraftTitle={setDraftTitle}
              hoveredConversationId={hoveredConversationId}
              setHoveredConversationId={setHoveredConversationId}
              focusedConversationId={focusedConversationId}
              setFocusedConversationId={setFocusedConversationId}
              confirmingDeleteId={confirmingDeleteId}
              setConfirmingDeleteId={setConfirmingDeleteId}
              deletingConversationId={deletingConversationId}
              onNewConversation={newConversation}
              onOpenConversation={(id) => {
                onNavigateChat?.();
                if (id === currentConversationId && sending) return;
                void openConversation(id);
              }}
              onPatchConversation={(id, patch) =>
                void patchConversation(id, patch)
              }
              onSaveTitle={saveTitle}
              onDeleteConversation={(id) => void deleteConversation(id)}
              onToggleArchived={() => setShowArchived(!showArchived)}
            />
          )}
          <div className="chat-conversation">
            <header className="chat-header">
              <div className="chat-header-title">
                <strong>
                  {currentConversation?.title || t.newConversation}
                </strong>
                <div className="chat-header-meta">
                  {showHeaderProjectBadge && (
                    <span className="chat-header-project">
                      {headerProjectName}
                    </span>
                  )}
                  <span>
                    {activeCatalogAgent?.name || currentAgent?.name || agentId}{" "}
                    · {activeProvider?.name || providerId || "—"} ·{" "}
                    {model || "—"}
                  </span>
                </div>
              </div>
            </header>
            {currentConversation?.archived && (
              <div className="archive-banner">
                {t.archived} ·{" "}
                <button
                  type="button"
                  onClick={() =>
                    void patchConversation(currentConversation.id, {
                      archived: false,
                    })
                  }
                >
                  {t.restore}
                </button>
              </div>
            )}
            <div className="chat-scroll">
              {error && <Alert type="error" title={error} />}
              {feedbackNotice && (
                <Alert
                  type="success"
                  title={feedbackNotice}
                  closable
                  onClose={() => setFeedbackNotice("")}
                />
              )}
              {!resumable && <Alert type="info" title={t.sessionExpired} />}
              {conversationAgentDisabled && (
                <Alert
                  className="chat-resumption-banner"
                  type="warning"
                  showIcon
                  title={t.chatDisabledAgentBanner}
                  action={
                    <Button
                      size="small"
                      type="primary"
                      onClick={newConversation}
                    >
                      {t.chatStartNewConversation}
                    </Button>
                  }
                />
              )}
              {!conversationAgentDisabled && conversationModelInvalid && (
                <Alert
                  className="chat-resumption-banner"
                  type="warning"
                  showIcon
                  title={t.chatInvalidModelBanner.replace(
                    "{model}",
                    boundConversation?.model ||
                      currentConversation?.model ||
                      "",
                  )}
                />
              )}
              {loadingConversation && (
                <p className="muted">{t.loadingConversation}</p>
              )}

              {turns.map((turn) => (
                <TurnBlock
                  key={turn.id}
                  turn={turn}
                  messages={t}
                  copied={copied}
                  onCopy={(sql) => void copySql(sql)}
                  onOpen={(ref) => {
                    void openCitation(ref);
                  }}
                  onClarify={chooseClarification}
                  onExecute={(turn, plan) => void executePlan(turn, plan)}
                  onDownload={(id, format) => void downloadQuery(id, format)}
                  onCopyLink={(id) => void copyDownloadLink(id)}
                  executing={sending}
                  expiredPlans={expiredPlans}
                  onMarkAccurate={(item) => void markTurnAccurate(item)}
                  onOpenCorrection={(item) => setCorrectionTurn(item)}
                  markingAccurate={markingTurnId === turn.id}
                  actionsDisabled={projectArchived || !currentConversationId}
                />
              ))}
              {live && (
                <article className="turn">
                  <div className="message">
                    <span className="avatar user" aria-hidden="true">
                      我
                    </span>
                    <div className="message-body">
                      <QuestionText text={live.question} messages={t} />
                    </div>
                  </div>
                  <div className="message">
                    <span className="avatar ai" aria-hidden="true">
                      AI
                    </span>
                    <div className="message-body">
                      <ToolList
                        tools={live.tools}
                        messages={t}
                        failed={turnFailed(live)}
                      />
                      <div className="answer">
                        {liveAnswer.text ? (
                          <Markdown text={liveAnswer.text} />
                        ) : (
                          <p className="muted">
                            {live.status === "running" ? t.asking : ""}
                          </p>
                        )}
                      </div>
                      <SqlList
                        blocks={live.sql}
                        messages={t}
                        copied={copied}
                        onCopy={(sql) => void copySql(sql)}
                      />
                      <CitationList
                        citations={liveCitations}
                        messages={t}
                        onOpen={(ref) => {
                          void openCitation(ref);
                        }}
                      />
                      <ClarificationList
                        groups={live.clarifications}
                        messages={t}
                        disabled={sending}
                        onSelect={chooseClarification}
                      />
                      {live.error && <Alert type="error" title={live.error} />}
                    </div>
                  </div>
                </article>
              )}
            </div>
            <Composer
              messages={t}
              question={question}
              setQuestion={setQuestion}
              onAsk={ask}
              currentConversation={currentConversation}
              currentConversationId={currentConversationId}
              resumable={resumable}
              emptyConversation={emptyConversation}
              projects={projects}
              projectId={projectId}
              projectArchived={projectArchived}
              selectProject={selectProject}
              projectLocked={projectLocked}
              agents={agents}
              agentId={agentId}
              selectAgent={selectAgent}
              profiles={profiles}
              providers={providers}
              providerId={providerId}
              selectProvider={selectProvider}
              model={model}
              activeProvider={activeProvider}
              onModelChange={selectModel}
              effort={effort}
              effortLevels={effortLevels}
              allowedEfforts={allowedEfforts}
              onEffortChange={selectEffort}
              canAsk={canAsk}
              conversationSelectionChanged={conversationSelectionChanged}
              attachments={attachments}
              attachmentError={attachmentError}
              addAttachments={addAttachments}
              removeAttachment={removeAttachment}
              visionEnabled={visionEnabled}
              onNavigateKnowledge={onNavigateKnowledge}
              availableSkills={availableSkills}
              selectedSkill={selectedSkill}
              onSelectSkill={setSelectedSkill}
              agentSupportsSkills={agentSupportsSkills}
              onNavigatePersonalSettings={onNavigatePersonalSettings}
              hasError={Boolean(error)}
              catalogMode={catalogMode}
              noPublishedAgents={noPublishedAgents}
              missingDefaultPrompt={missingDefaultPrompt}
              conversationAgentDisabled={conversationAgentDisabled}
              personalDefault={agentId ? personalDefaults[agentId] : undefined}
              personalDefaultNotice={personalDefaultNotice}
              personalDefaultError={personalDefaultError}
              savingPersonalDefault={savingPersonalDefault}
              onSavePersonalDefault={() => void savePersonalDefault()}
              onResetPersonalDefault={() =>
                void resetPersonalDefault(Boolean(currentConversationId))
              }
            />
          </div>
          {citationRef && (
            <ContextDrawer
              messages={t}
              citationRef={citationRef}
              citationHeading={citationHeading}
              citationDetail={citationDetail}
              selectedTable={selectedTable}
              citationLoading={citationLoading}
              citationError={citationError}
              token={token}
              onClose={closeCitation}
            />
          )}
          <CorrectionDraftDrawer
            open={correctionTurn !== null}
            token={token}
            project={projectId}
            messages={t}
            conversationId={currentConversationId || undefined}
            turnId={correctionTurn?.id}
            targetLabel={
              correctionTurn
                ? `${t.turnCount} #${correctionTurn.sequence}`
                : undefined
            }
            onClose={() => setCorrectionTurn(null)}
            onSubmitted={() => setFeedbackNotice(t.correctionSubmitted)}
          />
        </div>
      )}
      {settingsOnly && (
        <ProviderSettingsPage token={token} role={role} messages={t} />
      )}
    </section>
  );
}
