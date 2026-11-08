import { useRef, useState } from "react";
import type { Messages } from "../i18n";
import { loadTableDetail, tablePartsFromRef } from "../tableDetail";
import type { CitationDetail, DocumentView } from "./types";
import { copyText } from "./sse";

export function useCitations({
  token,
  messages: t,
  setError,
}: {
  token: string;
  messages: Messages;
  setError: (message: string) => void;
}) {
  const [copied, setCopied] = useState("");
  const [citationRef, setCitationRef] = useState("");
  const citationSequence = useRef(0);
  const [citationDetail, setCitationDetail] = useState<CitationDetail | null>(
    null,
  );
  const [citationError, setCitationError] = useState("");
  const [citationLoading, setCitationLoading] = useState(false);

  function resetCitation() {
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
  }

  async function openCitation(ref: string) {
    const sequence = ++citationSequence.current;
    setCitationRef(ref);
    setCitationDetail(null);
    setCitationError("");
    setCitationLoading(true);
    const type = ref.split(":", 1)[0];
    const value = ref.slice(ref.indexOf(":") + 1);
    try {
      if (type === "table" || type === "field") {
        const parts = tablePartsFromRef(ref);
        if (!parts) throw new Error();
        const table = await loadTableDetail(token, parts.database, parts.name);
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "table", ref, table });
      } else if (type === "document") {
        const docId = `document:${value.split("#", 1)[0]}`;
        const response = await fetch(
          `/api/v1/document?${new URLSearchParams({ doc_id: docId })}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok) throw new Error();
        const document = (await response.json()) as DocumentView;
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "document", ref, document });
      } else {
        setCitationDetail({
          kind: "message",
          ref,
          message: t.citationUnsupported,
        });
      }
    } catch {
      if (sequence === citationSequence.current)
        setCitationError(t.citationNotFound);
    } finally {
      if (sequence === citationSequence.current) setCitationLoading(false);
    }
  }

  async function copySql(sql: string) {
    try {
      await copyText(sql);
      setCopied(sql);
    } catch {
      setError(t.copyError);
    }
  }

  function closeCitation() {
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setCitationError("");
  }

  return {
    copied,
    citationRef,
    citationDetail,
    citationError,
    citationLoading,
    resetCitation,
    openCitation,
    copySql,
    closeCitation,
  };
}
