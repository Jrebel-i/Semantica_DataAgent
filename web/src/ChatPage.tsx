import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Input, Space, Tag, Typography } from "antd";
import type { components, paths } from "./api";
import type { Messages } from "./i18n";

type Agent = components["schemas"]["AgentView"];
type Provider = components["schemas"]["ProviderView"];
type Conversation = components["schemas"]["ConversationView"];
type ConversationDetail = components["schemas"]["ConversationDetail"];
type Turn = components["schemas"]["TurnView"];
type EventView = components["schemas"]["EventView"];
type TableRow =
  paths["/api/v1/tables"]["get"]["responses"][200]["content"]["application/json"][number];
type DocumentView =
  paths["/api/v1/document"]["get"]["responses"][200]["content"]["application/json"];

type SqlBlock = { sql: string; dialect?: string };
type Citation = { ref: string; type: string };
type ToolCall = {
  id: string;
  name: string;
  summary: string;
  status: "running" | "done" | "error";
  result?: string;
};
type Clarification = {
  id?: string;
  question?: string;
  options?: { id?: string; label?: string; ref?: string }[];
};
type LiveTurn = {
  question: string;
  text: string;
  tools: ToolCall[];
  sql: SqlBlock[];
  citations: Citation[];
  clarifications: Clarification[][];
  status: "running" | "completed" | "failed";
  error?: string;
};
type CitationDetail =
  | { kind: "table"; ref: string; table: TableRow }
  | { kind: "document"; ref: string; document: DocumentView }
  | { kind: "message"; ref: string; message: string };

const PROVIDER_TYPES = [
  "anthropic-messages",
  "openai-completions",
  "openai-responses",
  "google-generative-ai",
];

function sqlBlocks(values: unknown[] | undefined): SqlBlock[] {
  const blocks: SqlBlock[] = [];
  for (const value of values ?? []) {
    if (
      typeof value === "object" &&
      value !== null &&
      "sql" in value &&
      typeof value.sql === "string"
    ) {
      const dialect = "dialect" in value ? value.dialect : undefined;
      blocks.push({
        sql: value.sql,
        dialect: typeof dialect === "string" ? dialect : undefined,
      });
    }
  }
  return blocks;
}

function citationsOf(values: unknown[] | undefined): Citation[] {
  const result: Citation[] = [];
  for (const value of values ?? []) {
    if (
      typeof value === "object" &&
      value !== null &&
      "ref" in value &&
      typeof value.ref === "string"
    ) {
      const type = "type" in value ? value.type : undefined;
      result.push({
        ref: value.ref,
        type: typeof type === "string" ? type : value.ref.split(":", 1)[0],
      });
    }
  }
  return result;
}

function clarificationsOf(value: unknown): Clarification[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Clarification => typeof item === "object" && item !== null,
  );
}

function applyToolStart(
  tools: ToolCall[],
  data: Record<string, unknown>,
): ToolCall[] {
  const id = String(data.id ?? data.name ?? `${Date.now()}-${Math.random()}`);
  return [
    ...tools,
    {
      id,
      name: String(data.name ?? "tool"),
      summary: String(data.summary ?? ""),
      status: "running",
    },
  ];
}

function applyToolEnd(
  tools: ToolCall[],
  data: Record<string, unknown>,
): ToolCall[] {
  const id = String(data.id ?? data.name ?? "");
  const next: ToolCall = {
    id,
    name: String(data.name ?? "tool"),
    summary: String(data.summary ?? ""),
    status: data.is_error ? "error" : "done",
    result: String(data.summary ?? ""),
  };
  const index = tools.findIndex((tool) => tool.id === id);
  const result = [...tools];
  if (index >= 0) result[index] = { ...result[index], ...next };
  else result.push(next);
  return result;
}

function toolsOfTurn(events: EventView[]): ToolCall[] {
  let tools: ToolCall[] = [];
  for (const event of events) {
    if (event.type === "tool_start") tools = applyToolStart(tools, event.data);
    else if (event.type === "tool_end") tools = applyToolEnd(tools, event.data);
  }
  return tools;
}

function clarificationsOfTurn(events: EventView[]): Clarification[][] {
  const groups: Clarification[][] = [];
  for (const event of events) {
    if (event.type !== "clarification") continue;
    const questions = clarificationsOf(event.data.questions);
    if (questions.length > 0) groups.push(questions);
  }
  return groups;
}

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Plain HTTP deployments may deny the async clipboard API.
    }
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    area.remove();
  }
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

async function readSse(
  response: Response,
  onEvent: (event: string, data: Record<string, unknown>) => void,
) {
  if (!response.body) throw new Error("Streaming is not supported");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const parsed = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
        onEvent(parsed.event, parsed.data as Record<string, unknown>);
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  const parsed = parseSseBlock(buffer);
  if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
    onEvent(parsed.event, parsed.data as Record<string, unknown>);
  }
}

function ToolList({
  tools,
  messages,
}: {
  tools: ToolCall[];
  messages: Messages;
}) {
  if (tools.length === 0) return null;
  return (
    <section aria-label={messages.toolCalls}>
      <Typography.Title level={5}>{messages.toolCalls}</Typography.Title>
      <ul>
        {tools.map((tool) => (
          <li key={tool.id}>
            <Tag
              color={
                tool.status === "running"
                  ? "processing"
                  : tool.status === "error"
                    ? "error"
                    : "success"
              }
            >
              {tool.status === "running"
                ? messages.toolRunning
                : tool.status === "error"
                  ? messages.toolFailed
                  : messages.toolDone}
            </Tag>
            <code>{tool.name}</code> {tool.summary}
          </li>
        ))}
      </ul>
    </section>
  );
}

function SqlList({
  blocks,
  messages,
  copied,
  onCopy,
}: {
  blocks: SqlBlock[];
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
}) {
  if (blocks.length === 0) return null;
  return (
    <section aria-label={messages.sqlBlocks}>
      <Typography.Title level={5}>{messages.sqlBlocks}</Typography.Title>
      {blocks.map((block, index) => (
        <div key={`${index}-${block.sql.slice(0, 40)}`}>
          {block.dialect && <Tag>{block.dialect}</Tag>}
          <pre aria-label={messages.sqlBlocks}>
            <code>{block.sql}</code>
          </pre>
          <Button onClick={() => onCopy(block.sql)}>
            {copied === block.sql ? messages.sqlCopied : messages.copySql}
          </Button>
        </div>
      ))}
    </section>
  );
}

function CitationList({
  citations,
  messages,
  onOpen,
}: {
  citations: Citation[];
  messages: Messages;
  onOpen: (ref: string) => void;
}) {
  if (citations.length === 0) return null;
  return (
    <section aria-label={messages.citations}>
      <Typography.Title level={5}>{messages.citations}</Typography.Title>
      <Space wrap>
        {citations.map((citation) => (
          <Button
            key={citation.ref}
            type="link"
            onClick={() => onOpen(citation.ref)}
          >
            {citation.ref}
          </Button>
        ))}
      </Space>
    </section>
  );
}

function ClarificationList({
  groups,
  messages,
  disabled,
  onSelect,
}: {
  groups: Clarification[][];
  messages: Messages;
  disabled: boolean;
  onSelect: (
    question: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) => void;
}) {
  if (groups.length === 0) return null;
  return (
    <section aria-label={messages.clarification}>
      <Typography.Title level={5}>{messages.clarification}</Typography.Title>
      <p>{messages.clarificationHint}</p>
      {groups.map((questions, index) => (
        <div key={index}>
          {questions.map((question, questionIndex) => (
            <article key={question.id ?? questionIndex}>
              <p>{question.question}</p>
              <Space wrap>
                {(question.options ?? []).map((option, optionIndex) => (
                  <Button
                    key={
                      option.id ??
                      `${option.label}-${option.ref}-${optionIndex}`
                    }
                    disabled={disabled}
                    onClick={() => onSelect(question, option)}
                  >
                    <span>{option.label}</span>
                    {option.ref && (
                      <>
                        {" "}
                        <code>{option.ref}</code>
                      </>
                    )}
                  </Button>
                ))}
              </Space>
            </article>
          ))}
        </div>
      ))}
    </section>
  );
}

function TurnBlock({
  turn,
  messages,
  copied,
  onCopy,
  onOpen,
  onClarify,
}: {
  turn: Turn;
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
  onOpen: (ref: string) => void;
  onClarify: (
    question: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) => void;
}) {
  const sql = sqlBlocks(turn.sql);
  const citations = citationsOf(turn.citations);
  const tools = toolsOfTurn(turn.events ?? []);
  const clarifications = clarificationsOfTurn(turn.events ?? []);
  return (
    <article>
      <Typography.Title level={4}>{turn.question}</Typography.Title>
      <ToolList tools={tools} messages={messages} />
      <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
        {turn.answer}
      </Typography.Paragraph>
      {turn.error && <Alert type="error" title={turn.error} />}
      <SqlList
        blocks={sql}
        messages={messages}
        copied={copied}
        onCopy={onCopy}
      />
      <CitationList citations={citations} messages={messages} onOpen={onOpen} />
      <ClarificationList
        groups={clarifications}
        messages={messages}
        disabled={false}
        onSelect={onClarify}
      />
    </article>
  );
}

export function ChatPage({
  token,
  role,
  messages: t,
}: {
  token: string;
  role: string;
  messages: Messages;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [agentId, setAgentId] = useState("pi");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [currentConversationId, setCurrentConversationId] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [citationRef, setCitationRef] = useState("");
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

  const refreshConversations = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/conversations", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      setConversations((await response.json()) as Conversation[]);
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
        setAgents(agentRows);
        const available = agentRows.find((agent) => agent.available);
        if (available) setAgentId(available.id);
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
    void refreshConversations();
  }, [refreshConversations]);

  const activeProvider = providers.find((item) => item.id === providerId);

  function selectProvider(value: string) {
    setProviderId(value);
    const provider = providers.find((item) => item.id === value);
    setModel(provider?.models[0] || "");
  }

  function updateLive(change: (current: LiveTurn) => LiveTurn) {
    setLive((current) => (current ? change(current) : current));
  }

  async function submitPrompt(prompt: string) {
    const trimmed = prompt.trim();
    if (!trimmed || !providerId || !model || sending) return;
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
    const label = option.label?.trim() ?? "";
    const ref = option.ref?.trim() ?? "";
    if (!label || !ref) return;
    const prompt = [
      "澄清选择：请只针对当前问题按所选口径继续回答，不要把它作为全局规则。",
      `问题：${clarification.question ?? ""}`,
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
      setCurrentConversationId(detail.id);
      setTurns(detail.turns);
      setLive(null);
      setAgentId(detail.agent);
      setProviderId(detail.provider_id);
      setModel(detail.model);
    } catch {
      setError(t.askError);
    } finally {
      setLoadingConversation(false);
    }
  }

  function newConversation() {
    setCurrentConversationId("");
    setTurns([]);
    setLive(null);
    setError("");
  }

  async function openCitation(ref: string) {
    setCitationRef(ref);
    setCitationDetail(null);
    setCitationError("");
    setCitationLoading(true);
    const type = ref.split(":", 1)[0];
    const value = ref.slice(ref.indexOf(":") + 1);
    try {
      if (type === "table" || type === "field") {
        const tableName =
          type === "field" ? value.split(".").slice(0, 2).join(".") : value;
        const response = await fetch(
          `/api/v1/tables?${new URLSearchParams({ search: tableName })}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok) throw new Error();
        const rows = (await response.json()) as TableRow[];
        const table =
          rows.find((row) => row.ref === ref) ??
          rows.find((row) => `${row.database}.${row.name}` === tableName) ??
          rows[0];
        if (!table) {
          setCitationDetail({
            kind: "message",
            ref,
            message: t.citationNotFound,
          });
        } else {
          setCitationDetail({ kind: "table", ref, table });
        }
      } else if (type === "document") {
        const docId = value.split("#", 1)[0];
        const response = await fetch(
          `/api/v1/document?${new URLSearchParams({ doc_id: docId })}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok) throw new Error();
        setCitationDetail({
          kind: "document",
          ref,
          document: (await response.json()) as DocumentView,
        });
      } else {
        setCitationDetail({
          kind: "message",
          ref,
          message: t.citationUnsupported,
        });
      }
    } catch {
      setCitationError(t.citationNotFound);
    } finally {
      setCitationLoading(false);
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

  const canAsk = Boolean(question.trim() && providerId && model && !sending);

  return (
    <section>
      <Typography.Title level={2}>{t.chat}</Typography.Title>
      {error && <Alert type="error" title={error} />}
      <Space wrap>
        <Button onClick={newConversation} disabled={sending}>
          {t.newConversation}
        </Button>
        <Button
          onClick={() => void refreshConversations()}
          disabled={sending || loadingConversation}
        >
          {t.refresh}
        </Button>
      </Space>
      <div>
        <Typography.Title level={4}>{t.conversationList}</Typography.Title>
        {conversations.length === 0 ? (
          <p>{t.noConversations}</p>
        ) : (
          <ul>
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <Button
                  type="link"
                  onClick={() => void openConversation(conversation.id)}
                  disabled={sending}
                >
                  {conversation.title}
                </Button>
                <span>
                  {" "}
                  {conversation.turn_count} {t.turnCount}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <form onSubmit={(event) => void ask(event)}>
        <Space wrap>
          <label>
            {t.askAgent}{" "}
            <select
              aria-label={t.askAgent}
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
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
          <label>
            {t.askProvider}{" "}
            <select
              aria-label={t.askProvider}
              value={providerId}
              onChange={(event) => selectProvider(event.target.value)}
            >
              {providers.length === 0 && (
                <option value="">{t.noProviders}</option>
              )}
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t.askModel}{" "}
            <select
              aria-label={t.askModel}
              value={model}
              onChange={(event) => setModel(event.target.value)}
              disabled={!activeProvider}
            >
              {(activeProvider?.models ?? []).map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        </Space>
        <label style={{ display: "block", marginTop: 12 }}>
          {t.askQuestion}
          <Input.TextArea
            aria-label={t.askQuestion}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={3}
          />
        </label>
        <Button
          htmlType="submit"
          type="primary"
          disabled={!canAsk}
          style={{ marginTop: 8 }}
        >
          {sending ? t.asking : t.askSubmit}
        </Button>
      </form>

      {loadingConversation && <p>{t.loadingConversation}</p>}
      {turns.map((turn) => (
        <TurnBlock
          key={turn.id}
          turn={turn}
          messages={t}
          copied={copied}
          onCopy={(sql) => void copySql(sql)}
          onOpen={(ref) => void openCitation(ref)}
          onClarify={chooseClarification}
        />
      ))}
      {live && (
        <article>
          <Typography.Title level={4}>{live.question}</Typography.Title>
          <ToolList tools={live.tools} messages={t} />
          <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
            {live.text || (live.status === "running" ? t.asking : "")}
          </Typography.Paragraph>
          <SqlList
            blocks={live.sql}
            messages={t}
            copied={copied}
            onCopy={(sql) => void copySql(sql)}
          />
          <CitationList
            citations={live.citations}
            messages={t}
            onOpen={(ref) => void openCitation(ref)}
          />
          <ClarificationList
            groups={live.clarifications}
            messages={t}
            disabled={sending}
            onSelect={chooseClarification}
          />
          {live.error && <Alert type="error" title={live.error} />}
        </article>
      )}

      {citationRef && (
        <section aria-label={t.citationPanel}>
          <Typography.Title level={5}>{t.citationPanel}</Typography.Title>
          <Space>
            <code>{citationRef}</code>
            <Button
              size="small"
              onClick={() => {
                setCitationRef("");
                setCitationDetail(null);
                setCitationError("");
              }}
            >
              {t.close}
            </Button>
          </Space>
          {citationLoading && <p>{t.loadingCitation}</p>}
          {citationError && <Alert type="error" title={citationError} />}
          {citationDetail?.kind === "table" && (
            <article>
              <Typography.Title level={5}>
                {citationDetail.table.database}.{citationDetail.table.name}
              </Typography.Title>
              <p>{citationDetail.table.comment}</p>
              <ul>
                {citationDetail.table.fields.map((field) => (
                  <li key={field.name}>
                    <code>{field.name}</code> {field.type} {field.comment}
                  </li>
                ))}
              </ul>
            </article>
          )}
          {citationDetail?.kind === "document" && (
            <article>
              <Typography.Title level={5}>
                {citationDetail.document.path}
              </Typography.Title>
              <pre style={{ whiteSpace: "pre-wrap" }}>
                {citationDetail.document.content}
              </pre>
            </article>
          )}
          {citationDetail?.kind === "message" && (
            <Alert type="info" title={citationDetail.message} />
          )}
        </section>
      )}

      <section>
        <Typography.Title level={3}>{t.providerSettings}</Typography.Title>
        {providerError && <Alert type="error" title={providerError} />}
        {providerNotice && <Alert type="success" title={providerNotice} />}
        {providers.length === 0 ? (
          <p>{t.noProviders}</p>
        ) : (
          <ul>
            {providers.map((provider) => (
              <li key={provider.id}>
                <strong>{provider.name}</strong> ({provider.type}){" "}
                <code>{provider.base_url}</code>{" "}
                <Tag color={provider.has_api_key ? "green" : "default"}>
                  {provider.has_api_key
                    ? t.providerConfigured
                    : t.providerNotConfigured}
                </Tag>
                {role === "admin" && (
                  <>
                    <Button type="link" onClick={() => editProvider(provider)}>
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
              </li>
            ))}
          </ul>
        )}
        {role === "admin" && (
          <form onSubmit={(event) => void saveProvider(event)}>
            <Typography.Title level={5}>
              {editingProviderId ? t.providerEdit : t.providerAdd}
            </Typography.Title>
            <Space wrap>
              <label>
                {t.providerName}{" "}
                <Input
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
              </label>
              <label>
                {t.providerType}{" "}
                <select
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
              </label>
            </Space>
            <label style={{ display: "block", marginTop: 8 }}>
              {t.providerBaseUrl}
              <Input
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
            </label>
            <label style={{ display: "block", marginTop: 8 }}>
              {t.providerApiKey}
              <Input.Password
                aria-label={t.providerApiKey}
                value={providerForm.api_key}
                onChange={(event) =>
                  setProviderForm({
                    ...providerForm,
                    api_key: event.target.value,
                  })
                }
              />
            </label>
            <Typography.Paragraph type="secondary">
              {t.providerApiKeyHint}
            </Typography.Paragraph>
            <label style={{ display: "block" }}>
              {t.providerModels}
              <Input
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
            </label>
            <Space style={{ marginTop: 8 }}>
              <Button htmlType="submit">
                {editingProviderId ? t.providerSave : t.providerAdd}
              </Button>
              {editingProviderId && (
                <Button onClick={resetProviderForm}>{t.cancel}</Button>
              )}
            </Space>
          </form>
        )}
      </section>
    </section>
  );
}
