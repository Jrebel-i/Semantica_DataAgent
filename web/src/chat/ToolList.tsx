import { useEffect, useState } from "react";
import type { Messages } from "../i18n";
import { toolLabel } from "./turnEvents";
import type { ToolCall } from "./types";

export function ToolList({
  tools,
  messages,
  failed,
}: {
  tools: ToolCall[];
  messages: Messages;
  failed: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const failures = tools.filter((tool) => tool.status === "error").length;
  useEffect(() => {
    if (failed) setExpanded(true);
  }, [failed]);
  if (tools.length === 0) return null;
  const running = tools.find((tool) => tool.status === "running");
  return (
    <section aria-label={messages.toolCalls} className="tool-log">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        已使用 {tools.length} 个工具 {expanded ? "▾" : "▸"}
        {failures > 0 && !expanded && (
          <span className="tool-failures">
            {messages.toolFailuresHint.replace("{count}", String(failures))}
          </span>
        )}
      </button>
      {running && <span>正在{toolLabel(running.name)}…</span>}
      {expanded && (
        <>
          {tools.map((tool) => (
            <details
              className="tool-step"
              key={tool.id}
              open={tool.status === "error" ? true : undefined}
            >
              <summary title={`${tool.name} ${tool.summary}`}>
                <span className="tool-name">{toolLabel(tool.name)}</span>
                <code className="tool-raw">{tool.name}</code>
                <span className="tool-summary">{tool.summary || "—"}</span>
                <span className={`tool-status ${tool.status}`}>
                  {tool.status === "running"
                    ? messages.toolRunning
                    : tool.status === "error"
                      ? messages.toolFailed
                      : messages.toolDone}
                </span>
              </summary>
              <dl className="tool-detail">
                <dt>{messages.parameters}</dt>
                <dd>{tool.input || tool.summary || "—"}</dd>
                <dt>{messages.result}</dt>
                <dd>{tool.result || tool.summary || "—"}</dd>
              </dl>
            </details>
          ))}
        </>
      )}
    </section>
  );
}
