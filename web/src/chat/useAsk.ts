import { useState } from "react";
import type { MutableRefObject, Dispatch, SetStateAction } from "react";
import type { FormEvent } from "react";
import type { Messages } from "../i18n";
import { citationsOf, sqlBlocks } from "./citations";
import type {
  Clarification,
  Conversation,
  LiveTurn,
  QueryPlan,
  Turn,
} from "./types";
import { readSse } from "./sse";
import { applyToolEnd, applyToolStart, clarificationsOf } from "./turnEvents";

export function useAsk({
  token,
  messages: t,
  agentId,
  providerId,
  model,
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
}: {
  token: string;
  messages: Messages;
  agentId: string;
  providerId: string;
  model: string;
  currentConversationId: string;
  setCurrentConversationId: Dispatch<SetStateAction<string>>;
  sending: boolean;
  setSending: Dispatch<SetStateAction<boolean>>;
  resumable: boolean;
  archivedConversations: Conversation[];
  selectionTouched: MutableRefObject<boolean>;
  askSequence: MutableRefObject<number>;
  resetCitation: () => void;
  setLive: Dispatch<SetStateAction<LiveTurn | null>>;
  setTurns: Dispatch<SetStateAction<Turn[]>>;
  setError: Dispatch<SetStateAction<string>>;
  setExpiredPlans: Dispatch<SetStateAction<string[]>>;
  refreshConversations: () => Promise<void>;
  openConversation: (id: string) => Promise<void>;
  loadCompletedConversation: (id: string, sequence: number) => Promise<void>;
}) {
  const [question, setQuestion] = useState("");

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
    resetCitation();
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

  const canAsk = Boolean(
    question.trim() &&
    providerId &&
    model &&
    !sending &&
    resumable &&
    !archivedConversations.some((row) => row.id === currentConversationId),
  );

  return {
    question,
    setQuestion,
    submitPrompt,
    ask,
    chooseClarification,
    canAsk,
    executePlan,
  };
}
