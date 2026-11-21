import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button, Input, Select } from "antd";
import {
  ArrowUpOutlined,
  CheckOutlined,
  CloseOutlined,
  FileOutlined,
  FolderOpenOutlined,
  PaperClipOutlined,
  PictureOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import type { AgentProfiles } from "../agentProfiles";
import type { Messages } from "../i18n";
import { formatAttachmentSize, type PendingAttachment } from "./attachments";
import { EFFORT_LEVELS } from "./effort";
import type {
  Agent,
  Conversation,
  EffortLevel,
  Project,
  Provider,
} from "./types";

export function Composer({
  messages,
  question,
  setQuestion,
  onAsk,
  currentConversation,
  currentConversationId,
  resumable,
  emptyConversation,
  projects,
  projectId,
  projectArchived,
  selectProject,
  projectLocked,
  agents,
  agentId,
  selectAgent,
  profiles,
  providers,
  providerId,
  selectProvider,
  model,
  activeProvider,
  onModelChange,
  effort,
  effortLevels,
  allowedEfforts,
  onEffortChange,
  canAsk,
  conversationSelectionChanged,
  attachments,
  attachmentError,
  addAttachments,
  removeAttachment,
  visionEnabled,
  onNavigateKnowledge,
}: {
  messages: Messages;
  question: string;
  setQuestion: (value: string) => void;
  onAsk: (event: FormEvent) => void;
  currentConversation?: Conversation;
  currentConversationId: string;
  resumable: boolean;
  emptyConversation: boolean;
  projects: Project[];
  projectId: string;
  projectArchived: boolean;
  selectProject: (value: string) => void;
  projectLocked: boolean;
  agents: Agent[];
  agentId: string;
  selectAgent: (value: string) => void;
  profiles: AgentProfiles;
  providers: Provider[];
  providerId: string;
  selectProvider: (value: string) => void;
  model: string;
  activeProvider?: Provider;
  onModelChange: (value: string) => void;
  effort: EffortLevel | "";
  effortLevels: EffortLevel[];
  allowedEfforts: EffortLevel[];
  onEffortChange: (value: EffortLevel | "") => void;
  canAsk: boolean;
  conversationSelectionChanged: boolean;
  attachments: PendingAttachment[];
  attachmentError: string;
  addAttachments: (files: File[]) => void;
  removeAttachment: (id: string) => void;
  visionEnabled: boolean;
  onNavigateKnowledge?: () => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const [plusOpen, setPlusOpen] = useState(false);
  const t = messages;

  useEffect(() => {
    if (!plusOpen) return;
    const handleMouseDown = (event: MouseEvent) => {
      if (
        !(event.target instanceof Element) ||
        !event.target.closest(".composer-plus-wrap")
      ) {
        setPlusOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPlusOpen(false);
    };
    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [plusOpen]);
  const effortLabels: Record<EffortLevel, string> = {
    low: t.effortLow,
    medium: t.effortMedium,
    high: t.effortHigh,
    extra: t.effortExtra,
    max: t.effortMax,
  };
  const modelOptions = (activeProvider?.models ?? []).map((item) => ({
    value: item,
    label: item,
    description: activeProvider?.model_capabilities?.[item]?.description ?? "",
  }));
  const effortOptions = EFFORT_LEVELS.map((level) => ({
    value: level,
    label: effortLabels[level],
    disabled: !allowedEfforts.includes(level),
    hint: level === "max" ? t.effortMaxHint : "",
  }));
  const currentProject = projects.find((project) => project.id === projectId);
  const activeProjects = projects.filter(
    (project) => project.status === "active",
  );
  const showProjectSelect = !projectArchived && activeProjects.length > 1;
  return (
    <div className="chat-composer">
      {emptyConversation && (
        <>
          <h1 className="chat-greeting">{t.chatGreeting}</h1>
          {currentProject && (
            <div className="chat-project-guide">
              <span className="chat-project-guide-badge">
                {currentProject.name} ·{" "}
                {currentProject.type === "document"
                  ? t.projectGuideDocument
                  : t.projectGuideWarehouse}
              </span>
              <a
                href="/knowledge"
                className="chat-project-guide-link"
                onClick={(event) => {
                  if (onNavigateKnowledge) {
                    event.preventDefault();
                    onNavigateKnowledge();
                  }
                }}
              >
                {t.browseProjectKnowledge}
              </a>
            </div>
          )}
        </>
      )}
      <form className="composer-box" onSubmit={(event) => onAsk(event)}>
        {attachments.length > 0 && (
          <div className="composer-attachments" aria-label={t.attachments}>
            {attachments.map((attachment) => (
              <div className="composer-attachment" key={attachment.id}>
                {attachment.isImage && attachment.previewUrl ? (
                  <img
                    className="composer-attachment-thumb"
                    src={attachment.previewUrl}
                    alt={attachment.file.name}
                  />
                ) : (
                  <FileOutlined className="composer-attachment-icon" />
                )}
                <div className="composer-attachment-text">
                  <span className="composer-attachment-name">
                    {attachment.file.name}
                  </span>
                  <span className="composer-attachment-size">
                    {formatAttachmentSize(attachment.file.size)}
                  </span>
                </div>
                <Button
                  type="text"
                  size="small"
                  htmlType="button"
                  aria-label={`${t.removeAttachment} ${attachment.file.name}`}
                  onClick={() => removeAttachment(attachment.id)}
                >
                  <CloseOutlined />
                </Button>
              </div>
            ))}
          </div>
        )}
        {attachmentError && (
          <div className="composer-attachment-error" role="alert">
            {attachmentError}
          </div>
        )}
        <Input.TextArea
          aria-label={t.askQuestion}
          value={question}
          placeholder={
            projectArchived
              ? t.projectArchivedNotice
              : currentConversation?.archived
                ? t.restoreFirst
                : t.askPlaceholder
          }
          autoSize={{ minRows: 1, maxRows: 5 }}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={
            !resumable ||
            projectArchived ||
            Boolean(currentConversation?.archived)
          }
        />
        <div className="composer-controls">
          <div className="composer-left">
            <div className="composer-plus-wrap">
              <button
                type="button"
                className={`composer-plus-btn${plusOpen ? " active" : ""}`}
                aria-label={t.composerMore}
                title={t.composerMore}
                aria-expanded={plusOpen}
                onClick={() => setPlusOpen((open) => !open)}
              >
                <PlusOutlined />
              </button>
              <input
                ref={fileInput}
                className="composer-file-input"
                type="file"
                multiple
                aria-label={t.attachFile}
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? []);
                  if (files.length) addAttachments(files);
                  event.target.value = "";
                }}
              />
              <input
                ref={imageInput}
                className="composer-file-input"
                type="file"
                accept="image/*"
                multiple
                aria-label={t.attachImage}
                disabled={!visionEnabled}
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? []);
                  if (files.length) addAttachments(files);
                  event.target.value = "";
                }}
              />
              {plusOpen && (
                <div
                  className={`composer-plus-menu${emptyConversation ? " composer-plus-menu--down" : " composer-plus-menu--up"}`}
                  role="menu"
                >
                  <button
                    type="button"
                    role="menuitem"
                    className="composer-plus-item"
                    onClick={() => {
                      setPlusOpen(false);
                      fileInput.current?.click();
                    }}
                  >
                    <PaperClipOutlined aria-hidden="true" />
                    <span className="composer-plus-item-label">
                      {t.attachFile}
                    </span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className="composer-plus-item"
                    disabled={!visionEnabled}
                    onClick={() => {
                      setPlusOpen(false);
                      imageInput.current?.click();
                    }}
                  >
                    <PictureOutlined aria-hidden="true" />
                    <span className="composer-plus-item-label">
                      {t.attachImage}
                    </span>
                  </button>
                  {!visionEnabled && (
                    <div className="composer-plus-hint">
                      {t.attachmentVisionHint}
                    </div>
                  )}
                  {showProjectSelect && (
                    <>
                      <div className="composer-plus-divider" />
                      <div className="composer-plus-section">
                        {t.switchProject}
                      </div>
                      {activeProjects.map((project) => (
                        <button
                          key={project.id}
                          type="button"
                          role="menuitem"
                          className="composer-plus-item"
                          disabled={projectLocked}
                          onClick={() => {
                            selectProject(project.id);
                            setPlusOpen(false);
                          }}
                        >
                          <FolderOpenOutlined aria-hidden="true" />
                          <span className="composer-plus-item-label">
                            {project.name}
                          </span>
                          {project.id === projectId && (
                            <CheckOutlined
                              className="composer-plus-check"
                              aria-hidden="true"
                            />
                          )}
                        </button>
                      ))}
                    </>
                  )}
                </div>
              )}
            </div>
            {showProjectSelect && (
              <select
                className="sr-only"
                tabIndex={-1}
                aria-label={t.projects}
                value={projectId}
                disabled={projectLocked}
                onChange={(event) => selectProject(event.target.value)}
              >
                {activeProjects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            )}
            <select
              className="sr-only"
              tabIndex={-1}
              aria-label={t.askProvider}
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
            <label>
              <span className="sr-only">{t.askAgent}</span>
              <select
                className="composer-ghost-select"
                aria-label={t.askAgent}
                title={currentConversationId ? t.agentLockedHint : t.askAgent}
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
            {!showProjectSelect && currentProject && (
              <span
                className="composer-project-label"
                title={currentProject.name}
              >
                {currentProject.name}
              </span>
            )}
            {showProjectSelect && currentProject && (
              <button
                type="button"
                className="composer-project-chip"
                disabled={projectLocked}
                title={projectLocked ? t.projectLockedHint : t.switchProject}
                onClick={() => setPlusOpen((open) => !open)}
              >
                {currentProject.name}
              </button>
            )}
          </div>
          <div className="composer-right">
            <label>
              <span className="sr-only">{t.askModel}</span>
              <Select
                aria-label={t.askModel}
                className="composer-select composer-model-select"
                classNames={{ popup: { root: "composer-select-dropdown" } }}
                value={model || undefined}
                onChange={(value) => onModelChange(value)}
                disabled={!activeProvider}
                options={modelOptions}
                popupMatchSelectWidth={false}
                optionRender={(option) => (
                  <div className="model-option">
                    <span className="model-option-name">{option.label}</span>
                    {option.data.description && (
                      <span
                        className="model-option-description"
                        title={option.data.description}
                      >
                        {option.data.description}
                      </span>
                    )}
                  </div>
                )}
              />
            </label>
            {effortLevels.length > 0 && (
              <label>
                <span className="sr-only">{t.askEffort}</span>
                <Select
                  aria-label={t.askEffort}
                  className="composer-select composer-effort-select"
                  classNames={{ popup: { root: "composer-select-dropdown" } }}
                  value={effort || undefined}
                  placeholder={t.effortDefault}
                  onChange={(value) => onEffortChange(value as EffortLevel)}
                  options={effortOptions}
                  popupMatchSelectWidth={false}
                  optionRender={(option) => (
                    <div className="effort-option">
                      <span>{option.label}</span>
                      {option.data.hint && (
                        <span className="effort-option-hint">
                          {option.data.hint}
                        </span>
                      )}
                    </div>
                  )}
                />
              </label>
            )}
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
        </div>
        {projectArchived && (
          <div className="composer-switch-hint" role="status">
            {t.projectArchivedNotice}
          </div>
        )}
        {(!visionEnabled ||
          (projectLocked && !projectArchived && activeProjects.length > 1) ||
          Boolean(currentConversationId)) && (
          <div className="composer-lock-hints">
            {projectLocked && !projectArchived && activeProjects.length > 1 && (
              <span className="chat-selector-hint">{t.projectLockedHint}</span>
            )}
            {currentConversationId && (
              <span className="chat-selector-hint">{t.agentLockedHint}</span>
            )}
            {!visionEnabled && (
              <span className="chat-selector-hint">
                {t.attachmentVisionHint}
              </span>
            )}
          </div>
        )}
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
            {(currentProject?.type === "document"
              ? t.documentExamples
              : t.examples
            )
              .slice(0, 3)
              .map((example) => (
                <Button
                  key={example}
                  disabled={
                    !resumable ||
                    projectArchived ||
                    Boolean(currentConversation?.archived)
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
  );
}
