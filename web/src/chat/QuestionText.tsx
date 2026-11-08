import { useEffect, useRef, useState } from "react";
import type { Messages } from "../i18n";

export function QuestionText({
  text,
  messages,
}: {
  text: string;
  messages: Messages;
}) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(text.split("\n").length > 6);
  const textRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    setExpanded(false);
    setOverflowing(text.split("\n").length > 6);
  }, [text]);
  useEffect(() => {
    const element = textRef.current;
    if (!element || expanded) return;
    const measure = () => {
      if (element.scrollHeight > element.clientHeight + 1) setOverflowing(true);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text, expanded]);
  return (
    <>
      <p ref={textRef} className={`question-text${expanded ? "" : " clamped"}`}>
        {text}
      </p>
      {(overflowing || expanded) && (
        <button
          type="button"
          className="question-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? messages.collapseMessage : messages.expandMessage}
        </button>
      )}
    </>
  );
}
