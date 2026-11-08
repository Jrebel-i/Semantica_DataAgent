import { Button, Modal } from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  InboxOutlined,
  MoreOutlined,
  PushpinFilled,
  PushpinOutlined,
} from "@ant-design/icons";
import type { Messages } from "../i18n";
import type { Conversation } from "./types";

function shortTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ConversationSidebar({
  messages,
  conversations,
  archivedConversations,
  showArchived,
  currentConversationId,
  sessionTarget,
  sessionError,
  sending,
  menuId,
  setMenuId,
  editingId,
  setEditingId,
  draftTitle,
  setDraftTitle,
  hoveredConversationId,
  setHoveredConversationId,
  focusedConversationId,
  setFocusedConversationId,
  confirmingDeleteId,
  setConfirmingDeleteId,
  deletingConversationId,
  onNewConversation,
  onOpenConversation,
  onPatchConversation,
  onSaveTitle,
  onDeleteConversation,
  onToggleArchived,
  onNavigateChat,
}: {
  messages: Messages;
  conversations: Conversation[];
  archivedConversations: Conversation[];
  showArchived: boolean;
  currentConversationId: string;
  sessionTarget?: HTMLElement | null;
  sessionError: string;
  sending: boolean;
  menuId: string;
  setMenuId: (id: string) => void;
  editingId: string;
  setEditingId: (id: string) => void;
  draftTitle: string;
  setDraftTitle: (title: string) => void;
  hoveredConversationId: string;
  setHoveredConversationId: (id: string) => void;
  focusedConversationId: string;
  setFocusedConversationId: (id: string) => void;
  confirmingDeleteId: string;
  setConfirmingDeleteId: (id: string) => void;
  deletingConversationId: string;
  onNewConversation: () => void;
  onOpenConversation: (id: string) => void;
  onPatchConversation: (
    id: string,
    patch: { title?: string; pinned?: boolean; archived?: boolean },
  ) => void;
  onSaveTitle: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onToggleArchived: () => void;
  onNavigateChat?: () => void;
}) {
  const t = messages;
  return (
    <aside className="chat-sessions">
      <div className="session-head">
        <h1 className="session-title">
          {showArchived
            ? t.archived
            : sessionTarget
              ? t.conversationHistory
              : t.chat}
        </h1>
        {!sessionTarget && (
          <Button
            size="small"
            type="primary"
            onClick={onNewConversation}
            disabled={sending}
          >
            {t.newConversation}
          </Button>
        )}
      </div>
      {sessionError && (
        <div className="session-delete-error" role="alert">
          {sessionError}
        </div>
      )}
      <div className="session-list">
        {(showArchived ? archivedConversations : conversations).length ===
          0 && <div className="session-empty">{t.noConversations}</div>}
        {(showArchived ? archivedConversations : conversations).map(
          (conversation, index, list) => {
            const active = conversation.id === currentConversationId;
            const revealed =
              hoveredConversationId === conversation.id ||
              focusedConversationId === conversation.id;
            const group = conversation.pinned ? t.pin : t.conversationHistory;
            return (
              <div key={conversation.id}>
                {!showArchived &&
                  conversations.some((row) => row.pinned) &&
                  (index === 0 ||
                    list[index - 1].pinned !== conversation.pinned) && (
                    <div className="session-group">{group}</div>
                  )}
                <div
                  className={`session-item${active ? " active" : ""}`}
                  onMouseEnter={() => setHoveredConversationId(conversation.id)}
                  onMouseLeave={() => setHoveredConversationId("")}
                  onFocus={() => setFocusedConversationId(conversation.id)}
                  onBlur={() => setFocusedConversationId("")}
                >
                  <div className="session-row">
                    {editingId === conversation.id ? (
                      <input
                        autoFocus
                        value={draftTitle}
                        maxLength={200}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        onBlur={() => onSaveTitle(conversation.id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") onSaveTitle(conversation.id);
                          if (e.key === "Escape") setEditingId("");
                        }}
                      />
                    ) : (
                      <button
                        type="button"
                        className="session-open"
                        title={conversation.title}
                        onClick={() => {
                          onNavigateChat?.();
                          void onOpenConversation(conversation.id);
                        }}
                        disabled={sending}
                      >
                        {conversation.pinned && (
                          <PushpinFilled aria-hidden="true" />
                        )}{" "}
                        {conversation.title}
                        {conversation.resumable === false &&
                          ` (${t.sessionExpired})`}
                      </button>
                    )}
                    {showArchived ? (
                      <>
                        <button
                          type="button"
                          className="session-archive-action"
                          onClick={() =>
                            void onPatchConversation(conversation.id, {
                              archived: false,
                            })
                          }
                        >
                          {t.restore}
                        </button>
                        <button
                          type="button"
                          className="session-archive-action"
                          onClick={() => setConfirmingDeleteId(conversation.id)}
                        >
                          {t.deleteAction}
                        </button>
                      </>
                    ) : (
                      <div className="session-menu-wrap">
                        <button
                          type="button"
                          className={`session-menu-toggle${revealed ? " visible" : ""}`}
                          aria-label={t.conversationMenu}
                          onClick={() =>
                            setMenuId(
                              menuId === conversation.id ? "" : conversation.id,
                            )
                          }
                        >
                          <MoreOutlined />
                        </button>
                        {menuId === conversation.id && (
                          <div className="session-menu">
                            <button
                              type="button"
                              onClick={() => {
                                setMenuId("");
                                setDraftTitle(conversation.title);
                                setEditingId(conversation.id);
                              }}
                            >
                              <EditOutlined aria-hidden="true" /> {t.rename}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setMenuId("");
                                void onPatchConversation(conversation.id, {
                                  pinned: !conversation.pinned,
                                });
                              }}
                            >
                              <PushpinOutlined aria-hidden="true" />{" "}
                              {conversation.pinned ? t.unpin : t.pin}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setMenuId("");
                                void onPatchConversation(conversation.id, {
                                  archived: true,
                                });
                              }}
                            >
                              <InboxOutlined aria-hidden="true" /> {t.archive}
                            </button>
                            <button
                              type="button"
                              className="danger"
                              onClick={() => {
                                setMenuId("");
                                setConfirmingDeleteId(conversation.id);
                              }}
                            >
                              <DeleteOutlined aria-hidden="true" />{" "}
                              {t.deleteAction}
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <span className="session-meta">
                    {shortTime(conversation.updated_at)}
                  </span>
                </div>
              </div>
            );
          },
        )}
        <button
          type="button"
          className="session-archive-link"
          onClick={() => onToggleArchived()}
        >
          {showArchived
            ? t.back
            : `${t.archived} (${archivedConversations.length})`}
        </button>
        <Modal
          open={Boolean(confirmingDeleteId)}
          title={t.deleteConversation}
          okText={t.confirmDeleteConversation}
          cancelText={t.cancelDeleteConversation}
          confirmLoading={Boolean(deletingConversationId)}
          onCancel={() => setConfirmingDeleteId("")}
          onOk={() => void onDeleteConversation(confirmingDeleteId)}
        />
      </div>
    </aside>
  );
}
