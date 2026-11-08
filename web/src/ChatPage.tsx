import { createPortal } from "react-dom";
import { ColumnLineage } from "./ColumnLineage";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Input, Tag } from "antd";
import type { Language, Messages } from "./i18n";
import { TaskLineage } from "./TaskLineage";
import {
  ArrowUpOutlined,
  CloseOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { parseAgentProfiles, type AgentProfiles } from "./agentProfiles";
import { Markdown } from "./Markdown";
import { safeDecodePath } from "./paths";
import { loadTableDetail, tablePartsFromRef } from "./tableDetail";
import {
  citationLabel,
  citationsFromRefs,
  citationsOf,
  sqlBlocks,
  stripCitationRefs,
} from "./chat/citations";
import type {
  Agent,
  Citation,
  CitationDetail,
  CitationGroup,
  CitationLabel,
  Clarification,
  Conversation,
  ConversationDetail,
  DocumentView,
  EventView,
  LiveTurn,
  Provider,
  QueryPlan,
  QueryResult,
  SqlBlock,
  ToolCall,
  Turn,
} from "./chat/types";
import { PROVIDER_TYPES } from "./chat/types";
import { copyText, readSse } from "./chat/sse";
import { ResultTable } from "./chat/ResultTable";
import { ToolList } from "./chat/ToolList";
import { SqlList } from "./chat/SqlList";
import { CitationList } from "./chat/CitationList";
import { ClarificationList } from "./chat/ClarificationList";
import { QuestionText } from "./chat/QuestionText";
import { TurnBlock } from "./chat/TurnBlock";
import { ConversationSidebar } from "./chat/ConversationSidebar";
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
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<AgentProfiles>({});
  const selectionTouched = useRef(false);
  const askSequence = useRef(0);
  const [defaults, setDefaults] = useState({
    agent: "pi",
    provider: "",
    model: "",
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [archivedConversations, setArchivedConversations] = useState<
    Conversation[]
  >([]);
  const [showArchived, setShowArchived] = useState(false);
  const [menuId, setMenuId] = useState("");
  const [editingId, setEditingId] = useState("");
  const [draftTitle, setDraftTitle] = useState("");
  const [hoveredConversationId, setHoveredConversationId] = useState("");
  const [focusedConversationId, setFocusedConversationId] = useState("");
  const [confirmingDeleteId, setConfirmingDeleteId] = useState("");
  const [deletingConversationId, setDeletingConversationId] = useState("");
  const [sessionError, setSessionError] = useState("");
  const [agentId, setAgentId] = useState("pi");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
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
  const [providerForm, setProviderForm] = useState({
    name: "",
    type: "anthropic-messages",
    base_url: "",
    api_key: "",
    models: "",
  });
  const [editingProviderId, setEditingProviderId] = useState("");
  const [providerError, setProviderError] = useState("");
  const [providerNotice, setProviderNotice] = useState("");

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

  const refreshConversations = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/conversations", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const rows = (await response.json()) as Conversation[];
      if (!Array.isArray(rows)) throw new Error("Invalid conversation list");
      setConversations(rows);
      const archivedResponse = await fetch(
        "/api/v1/conversations?archived=true",
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!archivedResponse.ok) throw new Error();
      const archivedRows: unknown = await archivedResponse.json();
      if (!Array.isArray(archivedRows))
        throw new Error("Invalid archived conversation list");
      setArchivedConversations(archivedRows);
    } catch {
      setError(t.askError);
    }
  }, [token, t.askError]);

  const loadProviders = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const data = (await response.json()) as Provider[];
      setProviders(data);
      setProviderId((current) => current || data[0]?.id || "");
      setModel((current) => current || data[0]?.models[0] || "");
    } catch {
      setProviderError(t.providerError);
    }
  }, [token, t.providerError]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/v1/agents", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
      fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
    ])
      .then(async ([agentsResponse, providersResponse]) => {
        if (!agentsResponse.ok || !providersResponse.ok) throw new Error();
        const agentRows = (await agentsResponse.json()) as Agent[];
        const providerRows = (await providersResponse.json()) as Provider[];
        if (!Array.isArray(agentRows) || !Array.isArray(providerRows))
          throw new Error("Invalid agent or provider list");
        setAgents(agentRows);
        const available = agentRows.find((agent) => agent.available);
        if (available && !selectionTouched.current)
          setAgentId((current) =>
            agentRows.some((agent) => agent.id === current && agent.available)
              ? current
              : available.id,
          );
        setProviders(providerRows);
        setProviderId((current) => current || providerRows[0]?.id || "");
        setModel((current) => current || providerRows[0]?.models[0] || "");
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(t.askError);
      });
    return () => controller.abort();
  }, [token, t.askError]);

  useEffect(() => {
    if (!settingsOnly) void refreshConversations();
  }, [refreshConversations, settingsOnly, refreshKey]);

  useEffect(() => {
    void fetch("/api/v1/assistant-defaults", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as {
          assistant_agent?: string | null;
          assistant_provider_id?: string | null;
          assistant_model?: string | null;
          assistant_agent_profiles?: string | null;
        };
        const parsedProfiles = parseAgentProfiles(
          data.assistant_agent_profiles,
        );
        setProfiles(parsedProfiles);
        const agent = data.assistant_agent || "pi";
        const profile = parsedProfiles[agent];
        const next = {
          agent,
          provider:
            profile?.default_provider || data.assistant_provider_id || "",
          model: profile?.default_model || data.assistant_model || "",
        };
        setDefaults(next);
        if (!selectionTouched.current) {
          setAgentId(next.agent);
          if (next.provider) setProviderId(next.provider);
          if (next.model) setModel(next.model);
        }
      })
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    if (selectionTouched.current || !providers.length) return;
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
  }, [providers, profiles, defaults]);

  const activeProvider = providers.find((item) => item.id === providerId);

  function selectAgent(value: string) {
    selectionTouched.current = true;
    setAgentId(value);
    const profile = profiles[value];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === profile?.default_provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      profile?.default_model && chosen?.models.includes(profile.default_model)
        ? profile.default_model
        : chosen?.models[0] || "",
    );
  }

  function selectProvider(value: string) {
    selectionTouched.current = true;
    setProviderId(value);
    const provider = providers.find((item) => item.id === value);
    setModel(provider?.models[0] || "");
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

  function editProvider(provider: Provider) {
    setEditingProviderId(provider.id);
    setProviderForm({
      name: provider.name,
      type: provider.type,
      base_url: provider.base_url,
      api_key: "",
      models: provider.models.join(", "),
    });
    setProviderError("");
    setProviderNotice("");
  }

  function resetProviderForm() {
    setEditingProviderId("");
    setProviderForm({
      name: "",
      type: "anthropic-messages",
      base_url: "",
      api_key: "",
      models: "",
    });
  }

  async function saveProvider(event: FormEvent) {
    event.preventDefault();
    setProviderError("");
    setProviderNotice("");
    const models = providerForm.models
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const payload: Record<string, unknown> = {
      name: providerForm.name.trim(),
      type: providerForm.type,
      base_url: providerForm.base_url.trim(),
      models,
    };
    if (providerForm.api_key.trim()) payload.api_key = providerForm.api_key;
    try {
      const response = await fetch(
        editingProviderId
          ? `/api/v1/providers/${editingProviderId}`
          : "/api/v1/providers",
        {
          method: editingProviderId ? "PUT" : "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) throw new Error();
      resetProviderForm();
      setProviderNotice(t.providerSaved);
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteProvider(provider: Provider) {
    if (!window.confirm(t.providerDeleteConfirm)) return;
    setProviderError("");
    try {
      const response = await fetch(`/api/v1/providers/${provider.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      if (providerId === provider.id) {
        setProviderId("");
        setModel("");
      }
      if (editingProviderId === provider.id) resetProviderForm();
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteConversation(id: string) {
    if (deletingConversationId) return;
    setDeletingConversationId(id);
    setSessionError("");
    try {
      const response = await fetch(
        `/api/v1/conversations/${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (response.status === 409) {
        setSessionError(t.deleteConversationRunning);
        return;
      }
      if (!response.ok) {
        setSessionError(t.operationFailed);
        return;
      }
      setConversations((current) =>
        current.filter((conversation) => conversation.id !== id),
      );
      if (id === currentConversationId) newConversation();
      await refreshConversations();
    } catch {
      setSessionError(t.operationFailed);
    } finally {
      setDeletingConversationId("");
      setConfirmingDeleteId("");
    }
  }

  async function patchConversation(
    id: string,
    patch: { title?: string; pinned?: boolean; archived?: boolean },
  ) {
    setSessionError("");
    try {
      const response = await fetch(
        `/api/v1/conversations/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(patch),
        },
      );
      if (response.status === 409) {
        setSessionError(t.deleteConversationRunning);
        return;
      }
      if (!response.ok) throw new Error();
      if (patch.archived && id === currentConversationId) newConversation();
      if (patch.archived === false) setShowArchived(false);
      await refreshConversations();
    } catch {
      setSessionError(t.operationFailed);
    }
  }

  function saveTitle(id: string) {
    if (editingId !== id) return;
    setEditingId("");
    void patchConversation(id, { title: draftTitle });
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
            <div className="chat-composer">
              {emptyConversation && (
                <h1 className="chat-greeting">{t.chatGreeting}</h1>
              )}
              <form
                className="composer-box"
                onSubmit={(event) => void ask(event)}
              >
                <Input.TextArea
                  aria-label={t.askQuestion}
                  value={question}
                  placeholder={
                    currentConversation?.archived
                      ? t.restoreFirst
                      : t.askPlaceholder
                  }
                  autoSize={{ minRows: 1, maxRows: 5 }}
                  onChange={(event) => setQuestion(event.target.value)}
                  disabled={
                    !resumable || Boolean(currentConversation?.archived)
                  }
                />
                <div className="composer-controls">
                  <div className="chat-selectors">
                    <label>
                      <select
                        aria-label={t.askAgent}
                        title={
                          currentConversationId ? t.agentLockedHint : t.askAgent
                        }
                        value={agentId}
                        disabled={Boolean(currentConversationId)}
                        onChange={(event) => selectAgent(event.target.value)}
                      >
                        {agents.map((agent) => (
                          <option
                            key={agent.id}
                            value={agent.id}
                            disabled={!agent.available}
                          >
                            {agent.name}
                            {agent.available ? "" : ` (${t.unavailable})`}
                          </option>
                        ))}
                      </select>
                    </label>
                    {currentConversationId && (
                      <span className="chat-selector-hint">
                        {t.agentLockedHint}
                      </span>
                    )}
                    <label>
                      <select
                        aria-label={t.askProvider}
                        title={t.askProvider}
                        value={providerId}
                        disabled={!providers.length}
                        onChange={(event) => selectProvider(event.target.value)}
                      >
                        {providers.length === 0 && (
                          <option value="">{t.noProviders}</option>
                        )}
                        {providers
                          .filter(
                            (provider) =>
                              !profiles[agentId]?.providers.length ||
                              profiles[agentId].providers.includes(provider.id),
                          )
                          .map((provider) => (
                            <option key={provider.id} value={provider.id}>
                              {provider.name}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      <select
                        aria-label={t.askModel}
                        title={t.askModel}
                        value={model}
                        onChange={(event) => {
                          selectionTouched.current = true;
                          setModel(event.target.value);
                        }}
                        disabled={!activeProvider}
                      >
                        {(activeProvider?.models ?? []).map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <Button
                    htmlType="submit"
                    type="primary"
                    className="send-button"
                    aria-label={t.askSubmit}
                    title={t.askSubmit}
                    disabled={!canAsk}
                  >
                    <ArrowUpOutlined />
                  </Button>
                </div>
                {conversationSelectionChanged && (
                  <div className="composer-switch-hint" role="status">
                    {t.nextMessageModel.replace("{model}", model)}
                  </div>
                )}
              </form>
              {emptyConversation && (
                <div className="chat-examples">
                  <span className="example-title">{t.exampleQuestions}</span>
                  <div className="example-chips">
                    {t.examples.slice(0, 3).map((example) => (
                      <Button
                        key={example}
                        disabled={
                          !resumable || Boolean(currentConversation?.archived)
                        }
                        onClick={() => setQuestion(example)}
                      >
                        {example}
                      </Button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
          {citationRef && (
            <aside className="chat-context">
              <div className="context-header">
                <div className="context-heading">
                  <strong title={citationHeading?.title}>
                    {citationHeading?.label ?? t.citations}
                  </strong>
                  {citationDetail?.kind === "document" && (
                    <div className="context-ref">
                      {safeDecodePath(citationDetail.document.path)}
                    </div>
                  )}
                </div>
                <Button
                  type="text"
                  aria-label={t.close}
                  onClick={closeCitation}
                >
                  <CloseOutlined />
                </Button>
              </div>
              <div className="context-body">
                {citationLoading && <p>{t.loadingCitation}</p>}
                {citationError && <Alert type="error" title={citationError} />}
                {selectedTable ? (
                  <div className="context-schema">
                    <div className="context-schema-head">
                      <div className="context-schema-title">
                        <strong>
                          {safeDecodePath(
                            `${selectedTable.database}.${selectedTable.name}`,
                          )}
                        </strong>
                        {selectedTable.comment && (
                          <span className="muted">{selectedTable.comment}</span>
                        )}
                      </div>
                    </div>
                    <div className="context-schema-body">
                      <table className="schema-table">
                        <thead>
                          <tr>
                            <th>{t.field}</th>
                            <th>{t.dataType}</th>
                            <th>{t.comment}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedTable.fields.map((field) => (
                            <tr key={field.name}>
                              <td>{field.name}</td>
                              <td>{field.type}</td>
                              <td>
                                {field.comment}
                                {field.partition ? ` · ${t.partition}` : ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {citationRef.startsWith("field:") ? (
                        <ColumnLineage
                          token={token}
                          table={`${selectedTable.database}.${selectedTable.name}`}
                          column={citationRef
                            .slice(6)
                            .split(".")
                            .slice(2)
                            .join(".")}
                        />
                      ) : (
                        <TaskLineage
                          token={token}
                          messages={t}
                          table={`${selectedTable.database}.${selectedTable.name}`}
                          showTasks={false}
                        />
                      )}
                    </div>
                  </div>
                ) : citationDetail?.kind === "document" ? (
                  <article className="context-document">
                    <div className="doc-content">
                      {safeDecodePath(citationDetail.document.path)
                        .toLowerCase()
                        .endsWith(".md") ? (
                        <Markdown text={citationDetail.document.content} />
                      ) : (
                        <pre>{citationDetail.document.content}</pre>
                      )}
                    </div>
                  </article>
                ) : (
                  <div className="empty-state">{t.emptyContext}</div>
                )}
                {citationDetail?.kind === "message" && (
                  <Alert type="info" title={citationDetail.message} />
                )}
              </div>
            </aside>
          )}
        </div>
      )}
      {settingsOnly && (
        <div className="stack">
          <div className="page-header">
            <div>
              <h1 className="page-title">{t.providerSettings}</h1>
              <p className="page-subtitle">{t.providerApiKeyHint}</p>
            </div>
          </div>
          {providerError && <Alert type="error" title={providerError} />}
          {providerNotice && <Alert type="success" title={providerNotice} />}
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.providerSettings}</h2>
              <span className="muted">{providers.length}</span>
            </div>
            {providers.length === 0 ? (
              <p className="muted">{t.noProviders}</p>
            ) : (
              <div className="provider-list">
                {providers.map((provider) => (
                  <div className="provider-card" key={provider.id}>
                    <div className="provider-line">
                      <strong>{provider.name}</strong>
                      <span className="subtle">{provider.type}</span>
                      <Tag color={provider.has_api_key ? "green" : "default"}>
                        {provider.has_api_key
                          ? t.providerConfigured
                          : t.providerNotConfigured}
                      </Tag>
                      <code className="muted">{provider.base_url}</code>
                      {role === "admin" && (
                        <>
                          <Button
                            type="link"
                            onClick={() => editProvider(provider)}
                          >
                            {t.providerEdit}
                          </Button>
                          <Button
                            type="link"
                            danger
                            onClick={() => void deleteProvider(provider)}
                          >
                            {t.providerDelete}
                          </Button>
                        </>
                      )}
                    </div>
                    <div className="subtle mono">
                      {provider.models.join(" · ")}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
          {role === "admin" && (
            <section className="card section-card">
              <form onSubmit={(event) => void saveProvider(event)}>
                <div className="card-title-row">
                  <h2 className="card-title">
                    {editingProviderId ? t.providerEdit : t.providerAdd}
                  </h2>
                </div>
                <div className="grid-2">
                  <div className="field">
                    <label className="field-label" htmlFor="provider-name">
                      {t.providerName}
                    </label>
                    <Input
                      id="provider-name"
                      aria-label={t.providerName}
                      value={providerForm.name}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          name: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-type">
                      {t.providerType}
                    </label>
                    <select
                      id="provider-type"
                      aria-label={t.providerType}
                      value={providerForm.type}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          type: event.target.value,
                        })
                      }
                    >
                      {PROVIDER_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-url">
                      {t.providerBaseUrl}
                    </label>
                    <Input
                      id="provider-url"
                      aria-label={t.providerBaseUrl}
                      value={providerForm.base_url}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          base_url: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-key">
                      {t.providerApiKey}
                    </label>
                    <Input.Password
                      id="provider-key"
                      aria-label={t.providerApiKey}
                      value={providerForm.api_key}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          api_key: event.target.value,
                        })
                      }
                    />
                    <span className="field-hint">{t.providerApiKeyHint}</span>
                  </div>
                  <div className="field" style={{ gridColumn: "1 / -1" }}>
                    <label className="field-label" htmlFor="provider-models">
                      {t.providerModels}
                    </label>
                    <Input
                      id="provider-models"
                      aria-label={t.providerModels}
                      value={providerForm.models}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          models: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                </div>
                <div className="save-bar">
                  <Button htmlType="submit" type="primary">
                    {editingProviderId ? t.providerSave : t.providerAdd}
                  </Button>
                  {editingProviderId && (
                    <Button onClick={resetProviderForm}>{t.cancel}</Button>
                  )}
                </div>
              </form>
            </section>
          )}
        </div>
      )}
    </section>
  );
}
