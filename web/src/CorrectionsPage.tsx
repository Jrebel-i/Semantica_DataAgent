import { useCallback, useEffect, useState } from "react";

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
}: {
  token: string;
  gaps?: boolean;
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
      setError("加载失败");
    }
  }, [gaps, token]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  async function review(action: "approve" | "reject") {
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
          body: action === "approve" ? JSON.stringify({ patch }) : undefined,
        },
      );
      if (!response.ok) throw new Error();
      setSelected(null);
      await refresh();
    } catch {
      setError("审批失败");
    }
  }
  return (
    <section>
      <h2>{gaps ? "知识缺口" : "纠错审批"}</h2>
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
                    修改内容
                    <textarea
                      value={patch}
                      onChange={(e) => setPatch(e.target.value)}
                    />
                  </label>
                  <button onClick={() => void review("approve")}>批准</button>
                  <button onClick={() => void review("reject")}>驳回</button>
                </>
              )}
            </article>
          )}
        </>
      )}
    </section>
  );
}
