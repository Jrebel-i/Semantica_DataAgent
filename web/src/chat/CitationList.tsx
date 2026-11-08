import { useState } from "react";
import type { Messages } from "../i18n";
import { mergeCitations } from "./citations";
import type { Citation, CitationGroup } from "./types";

export function CitationList({
  citations,
  messages,
  onOpen,
}: {
  citations: Citation[];
  messages: Messages;
  onOpen: (ref: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const groups = mergeCitations(citations);
  if (groups.length === 0) return null;
  const visible = expanded ? groups : groups.slice(0, 6);
  const countOf = (citation: CitationGroup) =>
    citation.label.kind === "table"
      ? messages.dataTables
      : citation.label.kind === "document"
        ? messages.knowledgeDocuments
        : citation.label.kind === "lineage"
          ? messages.contextLineage
          : messages.citations;
  return (
    <section aria-label={messages.citations}>
      <div className="citation-list">
        {visible.map((citation) => (
          <button
            type="button"
            className={`citation-chip ${citation.label.kind}`}
            key={citation.key}
            aria-label={citation.openRef}
            title={citation.label.title}
            onClick={() => onOpen(citation.openRef)}
          >
            <span className="citation-kind">{countOf(citation)}</span>
            {citation.label.category && (
              <span className="citation-category">
                {citation.label.category}
              </span>
            )}
            <span className="citation-label">{citation.label.label}</span>
          </button>
        ))}
        {groups.length > 6 && (
          <button
            type="button"
            className="citation-chip citation-more"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded
              ? messages.close
              : `${messages.showMore} ${groups.length - 6} ${messages.moreCount}`}
          </button>
        )}
      </div>
    </section>
  );
}
