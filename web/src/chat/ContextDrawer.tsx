import { Alert, Button } from "antd";
import { CloseOutlined } from "@ant-design/icons";
import { ColumnLineage } from "../ColumnLineage";
import type { Messages } from "../i18n";
import { Markdown } from "../Markdown";
import { safeDecodePath } from "../paths";
import { TaskLineage } from "../TaskLineage";
import type { TableDetail } from "../tableDetail";
import type { CitationDetail, CitationLabel } from "./types";

export function ContextDrawer({
  messages,
  citationRef,
  citationHeading,
  citationDetail,
  selectedTable,
  citationLoading,
  citationError,
  token,
  onClose,
}: {
  messages: Messages;
  citationRef: string;
  citationHeading: CitationLabel | null;
  citationDetail: CitationDetail | null;
  selectedTable: TableDetail | null;
  citationLoading: boolean;
  citationError: string;
  token: string;
  onClose: () => void;
}) {
  const t = messages;
  return (
    <aside className="chat-context">
      <div className="context-header">
        <div className="context-heading">
          <strong title={citationHeading?.title}>
            {citationHeading?.label ?? t.citations}
          </strong>
          {citationDetail?.kind === "document" && (
            <div className="context-ref">
              {safeDecodePath(citationDetail.document.path)}
            </div>
          )}
        </div>
        <Button type="text" aria-label={t.close} onClick={onClose}>
          <CloseOutlined />
        </Button>
      </div>
      <div className="context-body">
        {citationLoading && <p>{t.loadingCitation}</p>}
        {citationError && <Alert type="error" title={citationError} />}
        {selectedTable ? (
          <div className="context-schema">
            <div className="context-schema-head">
              <div className="context-schema-title">
                <strong>
                  {safeDecodePath(
                    `${selectedTable.database}.${selectedTable.name}`,
                  )}
                </strong>
                {selectedTable.comment && (
                  <span className="muted">{selectedTable.comment}</span>
                )}
              </div>
            </div>
            <div
              className="context-schema-body"
              tabIndex={0}
              role="region"
              aria-label={t.fields}
            >
              <table className="schema-table">
                <thead>
                  <tr>
                    <th>{t.field}</th>
                    <th>{t.dataType}</th>
                    <th>{t.comment}</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedTable.fields.map((field) => (
                    <tr key={field.name}>
                      <td>{field.name}</td>
                      <td>{field.type}</td>
                      <td>
                        {field.comment}
                        {field.partition ? ` · ${t.partition}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {citationRef.startsWith("field:") ? (
                <ColumnLineage
                  token={token}
                  table={`${selectedTable.database}.${selectedTable.name}`}
                  column={citationRef.slice(6).split(".").slice(2).join(".")}
                />
              ) : (
                <TaskLineage
                  token={token}
                  messages={t}
                  table={`${selectedTable.database}.${selectedTable.name}`}
                  showTasks={false}
                />
              )}
            </div>
          </div>
        ) : citationDetail?.kind === "document" ? (
          <article className="context-document">
            <div className="doc-content">
              {safeDecodePath(citationDetail.document.path)
                .toLowerCase()
                .endsWith(".md") ? (
                <Markdown text={citationDetail.document.content} />
              ) : (
                <pre>{citationDetail.document.content}</pre>
              )}
            </div>
          </article>
        ) : (
          <div className="empty-state">{t.emptyContext}</div>
        )}
        {citationDetail?.kind === "message" && (
          <Alert type="info" title={citationDetail.message} />
        )}
      </div>
    </aside>
  );
}
