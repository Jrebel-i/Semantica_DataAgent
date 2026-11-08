import { useLayoutEffect, useRef, useState } from "react";
import { Button } from "antd";
import type { Messages } from "../i18n";
import { citationDisplayName, citationLabel } from "./citations";
import { clarificationOptionIsUsable } from "./turnEvents";
import type { Clarification } from "./types";

function ClarificationOption({
  option,
  messages,
  disabled,
  onSelect,
}: {
  option: NonNullable<Clarification["options"]>[number];
  messages: Messages;
  disabled: boolean;
  onSelect: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const labelRef = useRef<HTMLSpanElement | null>(null);
  const source = option.ref ? citationLabel(option.ref) : undefined;

  useLayoutEffect(() => {
    if (expanded) return;
    const label = labelRef.current;
    if (!label) return;
    const measure = () =>
      setOverflowing(label.scrollHeight > label.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(label);
    return () => observer.disconnect();
  }, [expanded, option.label]);

  return (
    <div className="clarification-option-item">
      <Button
        className="clarification-option"
        disabled={disabled}
        onClick={onSelect}
      >
        <span className="clarification-option-content">
          <span
            ref={labelRef}
            className={
              expanded
                ? "clarification-option-label is-expanded"
                : "clarification-option-label"
            }
          >
            {option.label}
          </span>
          {source && (
            <span className="clarification-source" title={source.title}>
              {citationDisplayName(source)}
            </span>
          )}
        </span>
      </Button>
      {overflowing && (
        <button
          type="button"
          className="clarification-option-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? messages.collapseMessage : messages.expandMessage}
        </button>
      )}
    </div>
  );
}

export function ClarificationList({
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
    <section className="clarification-card" aria-label={messages.clarification}>
      <h4 className="context-section-title">{messages.clarification}</h4>
      <p className="muted">{messages.clarificationHint}</p>
      {groups.map((questions, index) => (
        <div key={index}>
          {questions.map((question, questionIndex) => (
            <article key={question.id ?? questionIndex}>
              <p>{question.question}</p>
              {(() => {
                const options = question.options ?? [];
                const usable = options.filter(clarificationOptionIsUsable);
                const lost =
                  options.length > 0 && usable.length < options.length;
                return (
                  <>
                    {usable.length > 0 && (
                      <div className="clarification-options">
                        {usable.map((option, optionIndex) => (
                          <ClarificationOption
                            key={
                              option.id ??
                              `${option.label}-${option.ref}-${optionIndex}`
                            }
                            option={option}
                            messages={messages}
                            disabled={disabled}
                            onSelect={() => onSelect(question, option)}
                          />
                        ))}
                      </div>
                    )}
                    {lost && (
                      <p className="muted clarification-options-lost">
                        {messages.clarificationOptionsLost}
                      </p>
                    )}
                  </>
                );
              })()}
            </article>
          ))}
        </div>
      ))}
    </section>
  );
}
