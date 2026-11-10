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
import type { Conversation, Project } from "./types";

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
  projects,
  projectFilter,
  onProjectFilterChange,
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
}: {
  messages: Messages;
  conversations: Conversation[];
  archivedConversations: Conversation[];
  projects: Project[];
  projectFilter: string;
  onProjectFilterChange: (project: string) => void;
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
}) {
  const t = messages;
  const filter = (rows: Conversation[]) =>
    projectFilter
      ? rows.filter((conversation) => conversation.project_id === projectFilter)
      : rows;
  const visibleConversations = filter(conversations);
  const visibleArchived = filter(archivedConversations);
  const visible = showArchived ? visibleArchived : visibleConversations;
  return (
    <aside className="chat-sessions">
      <div className="session-head">
        <div className="session-head-main">
          <h1 className="session-title">
            {showArchived
              ? t.archived
              : sessionTarget
                ? t.conversationHistory
                : t.chat}
          </h1>
          <select
            className="session-project-filter"
            aria-label={t.projectFilter}
            title={t.projectFilter}
            value={projectFilter}
            onChange={(event) => onProjectFilterChange(event.target.value)}
          >
            <option value="">{t.allProjects}</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>
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
        {visible.length === 0 && (
          <div className="session-empty">{t.noConversations}</div>
        )}
        {visible.map((conversation, index, list) => {
          const active = conversation.id === currentConversationId;
          const revealed =
            hoveredConversationId === conversation.id ||
            focusedConversationId === conversation.id;
          const group = conversation.pinned ? t.pin : t.conversationHistory;
          return (
            <div key={conversation.id}>
              {!showArchived &&
                visibleConversations.some((row) => row.pinned) &&
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
                    <div className="session-content">
                      <button
                        type="button"
                        className="session-open"
                        aria-label={conversation.title}
                        title={conversation.title}
                        onClick={() => {
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
                      <span
                        className="session-project"
                        title={conversation.project_name}
                      >
                        {conversation.project_name}
                      </span>
                    </div>
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
        })}
        <button
          type="button"
          className="session-archive-link"
          onClick={() => onToggleArchived()}
        >
          {showArchived ? t.back : `${t.archived} (${visibleArchived.length})`}
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
