import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";
import type { Language, Messages } from "./i18n";
import { PlusOutlined } from "@ant-design/icons";
import { Markdown } from "./Markdown";
import {
  citationLabel,
  citationsFromRefs,
  stripCitationRefs,
} from "./chat/citations";
import type { ConversationDetail, LiveTurn, Project, Turn } from "./chat/types";
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
import { ProviderSettings } from "./chat/ProviderSettings";
import { useProviders } from "./chat/useProviders";
import { useConversations } from "./chat/useConversations";
import { rememberProject, storedProject } from "./ProjectSelector";
import { useCitations } from "./chat/useCitations";
import { useAsk } from "./chat/useAsk";
import { turnFailed } from "./chat/turnEvents";

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
  onNavigateChat,
  refreshKey = 0,
}: {
  token: string;
  role: string;
  messages: Messages;
  settingsOnly?: boolean;
  sessionTarget?: HTMLElement | null;
  newTarget?: HTMLElement | null;
  onNavigateChat?: () => void;
  language?: Language;
  onChangeLanguage?: () => void;
  refreshKey?: number;
}) {
  const askSequence = useRef(0);
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
    if (projects.some((project) => project.id === projectId)) return;
    const fallback =
      projects.find((project) => project.id === "default") ?? projects[0];
    setProjectId(fallback.id);
    rememberProject(fallback.id);
  }, [projects, projectId]);

  function selectProject(value: string) {
    setProjectId(value);
    rememberProject(value);
  }

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
    agents,
    profiles,
    defaults,
    providers,
    agentId,
    providerId,
    model,
    effort,
    effortLevels,
    allowedEfforts,
    selectionTouched,
    providerForm,
    editingProviderId,
    providerError,
    providerNotice,
    probeResults,
    probeLoadingModel,
    probeError,
    activeProvider,
    setAgentId,
    setProviderId,
    setModel,
    setProviderForm,
    selectAgent,
    selectProvider,
    selectModel,
    selectEffort,
    loadProviders,
    editProvider,
    resetProviderForm,
    saveProvider,
    deleteProvider,
    probeProviderModel,
    applyProbeResult,
  } = useProviders({ token, messages: t, onError: setError });

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

  const {
    question,
    setQuestion,
    ask,
    chooseClarification,
    canAsk,
    executePlan,
    attachments,
    attachmentError,
    addAttachments,
    removeAttachment,
    clearAttachments,
  } = useAsk({
    token,
    messages: t,
    agentId,
    providerId,
    model,
    effort,
    projectId,
    visionEnabled,
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

  async function openConversation(id: string) {
    setLoadingConversation(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/conversations/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const detail = (await response.json()) as ConversationDetail;
      applyConversation(detail);
    } catch {
      setError(t.askError);
    } finally {
      setLoadingConversation(false);
    }
  }

  function applyConversation(detail: ConversationDetail) {
    selectionTouched.current = true;
    if (detail.project_id) {
      setProjectId(detail.project_id);
      rememberProject(detail.project_id);
    }
    setCurrentConversationId(detail.id);
    setResumable(detail.resumable !== false);
    setTurns(detail.turns);
    resetCitation();
    setLive(null);
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

  function newConversation() {
    selectionTouched.current = true;
    setCurrentConversationId("");
    setAgentId(defaults.agent);
    const profile = profiles[defaults.agent];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === defaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(defaults.model)
        ? defaults.model
        : chosen?.models[0] || "",
    );
    setResumable(true);
    setTurns([]);
    resetCitation();
    setLive(null);
    setError("");
    clearAttachments();
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
                disabled={sending}
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
                  void openConversation(id);
                }}
                onPatchConversation={(id, patch) =>
                  void patchConversation(id, patch)
                }
                onSaveTitle={saveTitle}
                onDeleteConversation={(id) => void deleteConversation(id)}
                onToggleArchived={() => setShowArchived(!showArchived)}
                onNavigateChat={onNavigateChat}
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
                void openConversation(id);
              }}
              onPatchConversation={(id, patch) =>
                void patchConversation(id, patch)
              }
              onSaveTitle={saveTitle}
              onDeleteConversation={(id) => void deleteConversation(id)}
              onToggleArchived={() => setShowArchived(!showArchived)}
              onNavigateChat={onNavigateChat}
            />
          )}
          <div className="chat-conversation">
            <header className="chat-header">
              <div className="chat-header-title">
                <strong>
                  {currentConversation?.title || t.newConversation}
                </strong>
                <span>
                  {agentId} · {activeProvider?.name || providerId || "—"} ·{" "}
                  {model || "—"}
                </span>
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
              {!resumable && <Alert type="info" title={t.sessionExpired} />}
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
        </div>
      )}
      {settingsOnly && (
        <ProviderSettings
          messages={t}
          role={role}
          providers={providers}
          providerError={providerError}
          providerNotice={providerNotice}
          providerForm={providerForm}
          setProviderForm={setProviderForm}
          editingProviderId={editingProviderId}
          probeResults={probeResults}
          probeLoadingModel={probeLoadingModel}
          probeError={probeError}
          onEditProvider={editProvider}
          onDeleteProvider={(provider) => void deleteProvider(provider)}
          onSaveProvider={saveProvider}
          onResetProviderForm={resetProviderForm}
          onProbeModel={probeProviderModel}
          onApplyProbe={applyProbeResult}
        />
      )}
    </section>
  );
}
