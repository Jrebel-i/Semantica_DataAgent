import { createPortal } from "react-dom";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button } from "antd";
import type { Language, Messages } from "./i18n";
import { PlusOutlined } from "@ant-design/icons";
import { Markdown } from "./Markdown";
import { loadTableDetail, tablePartsFromRef } from "./tableDetail";
import {
  citationLabel,
  citationsFromRefs,
  citationsOf,
  sqlBlocks,
  stripCitationRefs,
} from "./chat/citations";
import type {
  CitationDetail,
  Clarification,
  ConversationDetail,
  DocumentView,
  LiveTurn,
  QueryPlan,
  Turn,
} from "./chat/types";
import { copyText, readSse } from "./chat/sse";
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
import {
  applyToolEnd,
  applyToolStart,
  clarificationOptionIsUsable,
  clarificationsOfTurn,
  clarificationsOf,
  queryEvent,
  resultCountText,
  toolLabel,
  toolsOfTurn,
  turnFailed,
} from "./chat/turnEvents";

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
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [expiredPlans, setExpiredPlans] = useState<string[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState("");
  const [resumable, setResumable] = useState(true);
  const [sending, setSending] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [citationRef, setCitationRef] = useState("");
  const citationSequence = useRef(0);
  const [citationDetail, setCitationDetail] = useState<CitationDetail | null>(
    null,
  );
  const [citationError, setCitationError] = useState("");
  const [citationLoading, setCitationLoading] = useState(false);

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
    selectionTouched,
    providerForm,
    editingProviderId,
    providerError,
    providerNotice,
    activeProvider,
    setAgentId,
    setProviderId,
    setModel,
    setProviderForm,
    selectAgent,
    selectProvider,
    selectModel,
    loadProviders,
    editProvider,
    resetProviderForm,
    saveProvider,
    deleteProvider,
  } = useProviders({ token, messages: t, onError: setError });

  async function executePlan(turn: Turn, plan: QueryPlan) {
    if (sending || !currentConversationId) return;
    setSending(true);
    setError("");
    setTurns((current) =>
      current.map((item) =>
        item.id === turn.id ? { ...item, error: null } : item,
      ),
    );
    try {
      const response = await fetch(
        `/api/v1/conversations/${encodeURIComponent(currentConversationId)}/turns/${turn.id}/queries/${encodeURIComponent(plan.id)}/execute`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        if (response.status === 410) {
          setExpiredPlans((current) =>
            current.includes(plan.id) ? current : [...current, plan.id],
          );
        }
        throw new Error(body?.detail || t.askError);
      }
      await readSse(response, (name, data) => {
        if (name === "query_result") {
          setTurns((current) =>
            current.map((item) => {
              if (item.id !== turn.id) return item;
              const events = item.events ?? [];
              return {
                ...item,
                status: "running",
                events: [...events, { seq: events.length, type: name, data }],
              };
            }),
          );
        } else if (name === "done") {
          setTurns((current) =>
            current.map((item) =>
              item.id === turn.id
                ? {
                    ...item,
                    status: "completed",
                    answer:
                      typeof data.text === "string" ? data.text : item.answer,
                  }
                : item,
            ),
          );
        } else if (name === "error") {
          const message = String(data.message ?? t.answerFailed);
          setError(message);
          setTurns((current) =>
            current.map((item) =>
              item.id === turn.id ? { ...item, error: message } : item,
            ),
          );
        }
      });
      await openConversation(currentConversationId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : t.askError;
      setError(message);
      setTurns((current) =>
        current.map((item) =>
          item.id === turn.id ? { ...item, error: message } : item,
        ),
      );
    } finally {
      setSending(false);
    }
  }

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

  function updateLive(change: (current: LiveTurn) => LiveTurn) {
    setLive((current) => (current ? change(current) : current));
  }

  async function submitPrompt(prompt: string) {
    const trimmed = prompt.trim();
    if (
      !trimmed ||
      !providerId ||
      !model ||
      sending ||
      !resumable ||
      archivedConversations.some((row) => row.id === currentConversationId)
    )
      return;
    selectionTouched.current = true;
    const sequence = ++askSequence.current;
    let conversationId = currentConversationId;
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setSending(true);
    setError("");
    setLive({
      question: trimmed,
      text: "",
      tools: [],
      sql: [],
      citations: [],
      clarifications: [],
      status: "running",
    });
    try {
      const response = await fetch("/api/v1/conversations/ask", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: prompt,
          agent: agentId,
          provider_id: providerId,
          model,
          ...(currentConversationId
            ? { conversation_id: currentConversationId }
            : {}),
        }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        throw new Error(body?.detail || t.askError);
      }
      await readSse(response, (name, data) => {
        if (
          name === "conversation" &&
          typeof data.conversation_id === "string"
        ) {
          conversationId = data.conversation_id;
          setCurrentConversationId(data.conversation_id);
        } else if (name === "text_delta" && typeof data.text === "string") {
          updateLive((current) => ({
            ...current,
            text: current.text + String(data.text),
          }));
        } else if (name === "tool_start") {
          updateLive((current) => ({
            ...current,
            tools: applyToolStart(current.tools, data),
          }));
        } else if (name === "tool_end") {
          updateLive((current) => {
            return { ...current, tools: applyToolEnd(current.tools, data) };
          });
        } else if (name === "clarification") {
          const questions = clarificationsOf(data.questions);
          if (questions.length > 0) {
            updateLive((current) => ({
              ...current,
              clarifications: [...current.clarifications, questions],
            }));
          }
        } else if (name === "done") {
          updateLive((current) => ({
            ...current,
            text: typeof data.text === "string" ? data.text : current.text,
            sql: sqlBlocks(data.sql as unknown[] | undefined),
            citations: citationsOf(data.citations as unknown[] | undefined),
            status: "completed",
          }));
        } else if (name === "error") {
          updateLive((current) => ({
            ...current,
            status: "failed",
            error: String(data.message ?? t.answerFailed),
          }));
        }
      });
      await refreshConversations();
      if (conversationId) {
        await loadCompletedConversation(conversationId, sequence);
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : t.askError;
      setError(message);
      updateLive((current) => ({
        ...current,
        status: "failed",
        error: message,
      }));
    } finally {
      setSending(false);
    }
  }

  function ask(event: FormEvent) {
    event.preventDefault();
    const prompt = question.trim();
    setQuestion("");
    void submitPrompt(prompt);
  }

  function chooseClarification(
    clarification: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) {
    const id = option.id?.trim() ?? "";
    const label = option.label?.trim() ?? "";
    const ref = option.ref?.trim() ?? "";
    if (!label || !ref) return;
    const prompt = [
      "澄清选择：请只针对当前问题按所选口径继续回答，不要把它作为全局规则。",
      `问题：${clarification.question ?? ""}`,
      ...(id && id !== "…" ? [`选项 id：${id}`] : []),
      `选项：${label}`,
      `证据：${ref}`,
    ].join("\n");
    void submitPrompt(prompt);
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
    if (detail.id !== currentConversationId) {
    }
    setCurrentConversationId(detail.id);
    setResumable(detail.resumable !== false);
    setTurns(detail.turns);
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
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
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setLive(null);
    setError("");
  }

  async function openCitation(ref: string) {
    const sequence = ++citationSequence.current;
    setCitationRef(ref);
    setCitationDetail(null);
    setCitationError("");
    setCitationLoading(true);
    const type = ref.split(":", 1)[0];
    const value = ref.slice(ref.indexOf(":") + 1);
    try {
      if (type === "table" || type === "field") {
        const parts = tablePartsFromRef(ref);
        if (!parts) throw new Error();
        const table = await loadTableDetail(token, parts.database, parts.name);
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "table", ref, table });
      } else if (type === "document") {
        const docId = `document:${value.split("#", 1)[0]}`;
        const response = await fetch(
          `/api/v1/document?${new URLSearchParams({ doc_id: docId })}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok) throw new Error();
        const document = (await response.json()) as DocumentView;
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "document", ref, document });
      } else {
        setCitationDetail({
          kind: "message",
          ref,
          message: t.citationUnsupported,
        });
      }
    } catch {
      if (sequence === citationSequence.current)
        setCitationError(t.citationNotFound);
    } finally {
      if (sequence === citationSequence.current) setCitationLoading(false);
    }
  }

  async function copySql(sql: string) {
    try {
      await copyText(sql);
      setCopied(sql);
    } catch {
      setError(t.copyError);
    }
  }

  const canAsk = Boolean(
    question.trim() &&
    providerId &&
    model &&
    !sending &&
    resumable &&
    !archivedConversations.some((row) => row.id === currentConversationId),
  );

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
  const closeCitation = () => {
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setCitationError("");
  };
  const emptyConversation = !loadingConversation && turns.length === 0 && !live;
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
              canAsk={canAsk}
              conversationSelectionChanged={conversationSelectionChanged}
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
          onEditProvider={editProvider}
          onDeleteProvider={(provider) => void deleteProvider(provider)}
          onSaveProvider={saveProvider}
          onResetProviderForm={resetProviderForm}
        />
      )}
    </section>
  );
}
