import type { FormEvent } from "react";
import { Button, Input, Select } from "antd";
import { ArrowUpOutlined } from "@ant-design/icons";
import type { AgentProfiles } from "../agentProfiles";
import type { Messages } from "../i18n";
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
}) {
  const t = messages;
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
  return (
    <div className="chat-composer">
      {emptyConversation && <h1 className="chat-greeting">{t.chatGreeting}</h1>}
      <form className="composer-box" onSubmit={(event) => onAsk(event)}>
        <Input.TextArea
          aria-label={t.askQuestion}
          value={question}
          placeholder={
            currentConversation?.archived ? t.restoreFirst : t.askPlaceholder
          }
          autoSize={{ minRows: 1, maxRows: 5 }}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={!resumable || Boolean(currentConversation?.archived)}
        />
        <div className="composer-controls">
          <div className="chat-selectors">
            {projects.length === 1 && (
              <span className="composer-project-label" title={projects[0].name}>
                {projects[0].name}
              </span>
            )}
            {projects.length > 1 && (
              <label>
                <select
                  aria-label={t.projects}
                  title={projectLocked ? t.projectLockedHint : t.projects}
                  value={projectId}
                  disabled={projectLocked}
                  onChange={(event) => selectProject(event.target.value)}
                >
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {projectLocked && projects.length > 1 && (
              <span className="chat-selector-hint">{t.projectLockedHint}</span>
            )}
            <label>
              <select
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
            {currentConversationId && (
              <span className="chat-selector-hint">{t.agentLockedHint}</span>
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
              <span>{t.askModel}</span>
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
                <span>{t.askEffort}</span>
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
                disabled={!resumable || Boolean(currentConversation?.archived)}
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
