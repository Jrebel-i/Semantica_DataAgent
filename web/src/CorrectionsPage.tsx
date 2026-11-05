import { useCallback, useEffect, useState } from "react";
import type { Messages } from "./i18n";

type Proposal = {
  id: string;
  target: string;
  reason: string;
  diff: string;
  patch: string;
  status: string;
};
type Gap = { id: string; question: string; missing: string };

export function CorrectionsPage({
  token,
  gaps = false,
  messages: t,
}: {
  token: string;
  gaps?: boolean;
  messages: Messages;
}) {
  const [rows, setRows] = useState<Proposal[]>([]);
  const [missing, setMissing] = useState<Gap[]>([]);
  const [selected, setSelected] = useState<Proposal | null>(null);
  const [patch, setPatch] = useState("");
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch(
        gaps ? "/api/v1/knowledge-gaps" : "/api/v1/corrections",
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      if (gaps) setMissing((await response.json()) as Gap[]);
      else setRows((await response.json()) as Proposal[]);
      setError("");
    } catch {
      setError(t.correctionLoadError);
    }
  }, [gaps, token, t.correctionLoadError]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  async function review(action: "approve" | "reject", edited = false) {
    if (!selected) return;
    try {
      const response = await fetch(
        `/api/v1/corrections/${selected.id}/${action}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body:
            action === "approve" && edited
              ? JSON.stringify({ patch })
              : undefined,
        },
      );
      if (!response.ok) throw new Error();
      setSelected(null);
      await refresh();
    } catch {
      setError(t.correctionReviewError);
    }
  }
  return (
    <section>
      <h2>{gaps ? t.gapTitle : t.correctionTitle}</h2>
      {error && <p role="alert">{error}</p>}
      {gaps ? (
        missing.map((g) => (
          <article key={g.id}>
            <h3>{g.question}</h3>
            <p>{g.missing}</p>
          </article>
        ))
      ) : (
        <>
          <ul>
            {rows.map((row) => (
              <li key={row.id}>
                <button
                  onClick={() => {
                    setSelected(row);
                    setPatch(row.patch);
                  }}
                >
                  {row.target} — {row.status}
                </button>
              </li>
            ))}
          </ul>
          {selected && (
            <article>
              <h3>{selected.target}</h3>
              <p>{selected.reason}</p>
              <pre>{selected.diff}</pre>
              {selected.status === "pending" && (
                <>
                  <label>
                    {t.correctionPatch}
                    <textarea
                      value={patch}
                      onChange={(e) => setPatch(e.target.value)}
                    />
                  </label>
                  <button onClick={() => void review("approve")}>
                    {t.correctionApprove}
                  </button>
                  <button onClick={() => void review("approve", true)}>
                    {t.correctionApproveEdited}
                  </button>
                  <button onClick={() => void review("reject")}>
                    {t.correctionReject}
                  </button>
                </>
              )}
            </article>
          )}
        </>
      )}
    </section>
  );
}
