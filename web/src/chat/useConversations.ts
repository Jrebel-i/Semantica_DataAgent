import { useCallback, useEffect, useState } from "react";
import type { Messages } from "../i18n";
import type { Conversation } from "./types";

export function useConversations({
  token,
  messages,
  settingsOnly,
  refreshKey,
  currentConversationId,
  onNewConversation,
  onError,
}: {
  token: string;
  messages: Messages;
  settingsOnly: boolean;
  refreshKey: number;
  currentConversationId: string;
  onNewConversation: () => void;
  onError: (message: string) => void;
}) {
  const t = messages;
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
      onError(t.askError);
    }
  }, [token, t.askError, onError]);

  useEffect(() => {
    if (!settingsOnly) void refreshConversations();
  }, [refreshConversations, settingsOnly, refreshKey]);

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
      if (id === currentConversationId) onNewConversation();
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
      if (patch.archived && id === currentConversationId) onNewConversation();
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

  return {
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
  };
}
