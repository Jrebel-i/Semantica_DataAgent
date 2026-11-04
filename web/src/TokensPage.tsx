import { useCallback, useEffect, useState } from "react";
import type { Messages } from "./i18n";

type TokenRow = {
  id: string;
  name: string;
  role: "user" | "admin";
  created_at: string;
  last_used_at: string | null;
  revoked: boolean;
};

export function TokensPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [rows, setRows] = useState<TokenRow[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState("user");
  const [issued, setIssued] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/tokens", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      setRows((await response.json()) as TokenRow[]);
    } catch {
      setError(t.tokenError);
    }
  }, [token, t.tokenError]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  async function create() {
    try {
      const response = await fetch("/api/v1/tokens", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name, role }),
      });
      if (!response.ok) throw new Error();
      const result = (await response.json()) as { token: string };
      setIssued(result.token);
      setError("");
      void refresh();
    } catch {
      setError(t.tokenError);
    }
  }
  async function revoke(id: string) {
    try {
      const response = await fetch(`/api/v1/tokens/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        setError(response.status === 409 ? t.lastAdmin : t.tokenError);
        return;
      }
      setConfirm(null);
      setError("");
      void refresh();
    } catch {
      setError(t.tokenError);
    }
  }
  return (
    <section>
      <h2>{t.tokens}</h2>
      {error && <p role="alert">{error}</p>}
      <button
        onClick={() => {
          setOpen(true);
          setName("");
          setRole("user");
          setError("");
        }}
      >
        {t.issue}
      </button>
      <table>
        <thead>
          <tr>
            {[t.name, t.role, t.created, t.lastUsed, t.status, ""].map(
              (heading, i) => (
                <th key={i}>{heading}</th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{row.name}</td>
              <td>{row.role}</td>
              <td>
                {new Date(
                  row.created_at.endsWith("Z")
                    ? row.created_at
                    : row.created_at + "Z",
                ).toLocaleString()}
              </td>
              <td>
                {row.last_used_at
                  ? new Date(
                      row.last_used_at.endsWith("Z")
                        ? row.last_used_at
                        : row.last_used_at + "Z",
                    ).toLocaleString()
                  : t.never}
              </td>
              <td>{row.revoked ? t.revoked : t.active}</td>
              <td>
                {!row.revoked && (
                  <button onClick={() => setConfirm(row.id)}>{t.revoke}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && (
        <div role="dialog" aria-modal="true" aria-label={t.issue}>
          {issued ? (
            <>
              <p>{t.once}</p>
              <code>{issued}</code>
              <button
                onClick={() => void navigator.clipboard.writeText(issued)}
              >
                {t.copy}
              </button>
            </>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <label>
                {t.name}
                <input
                  required
                  maxLength={200}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label>
                {t.role}
                <select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <button type="submit">{t.create}</button>
            </form>
          )}
          <button
            onClick={() => {
              setOpen(false);
              setIssued("");
            }}
          >
            {t.close}
          </button>
        </div>
      )}
      {confirm && (
        <div role="dialog" aria-modal="true" aria-label={t.confirmRevoke}>
          <p>{t.confirmRevoke}?</p>
          <button onClick={() => void revoke(confirm)}>
            {t.confirmRevoke}
          </button>
          <button onClick={() => setConfirm(null)}>{t.cancel}</button>
        </div>
      )}
    </section>
  );
}
