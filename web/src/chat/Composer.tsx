import type { FormEvent } from "react";
import { Button, Input } from "antd";
import { ArrowUpOutlined } from "@ant-design/icons";
import type { AgentProfiles } from "../agentProfiles";
import type { Messages } from "../i18n";
import type { Agent, Conversation, Provider } from "./types";

export function Composer({
  messages,
  question,
  setQuestion,
  onAsk,
  currentConversation,
  currentConversationId,
  resumable,
  emptyConversation,
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
  canAsk: boolean;
  conversationSelectionChanged: boolean;
}) {
  const t = messages;
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
              <select
                aria-label={t.askModel}
                title={t.askModel}
                value={model}
                onChange={(event) => onModelChange(event.target.value)}
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
